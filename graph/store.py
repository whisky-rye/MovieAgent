"""知识图谱存储与多跳遍历（F1/O12）。

设计要点：
- 与具体数据源解耦：遍历逻辑只依赖 ``GraphFetcher`` 接口（``relations_from`` /
  ``movie_meta`` / ``person_label`` / ``search_person``）。默认实现 ``MysqlGraphFetcher``
  走 MySQL 关系表；单元测试用假 fetcher 注入，无需数据库。
- 邻接缓存：每个实体的出边按需查询并缓存（带容量上限 + FIFO 淘汰），单请求内复用，
  避免重复打库；生产期可整库替换为 Neo4j（接口不变，见架构文档第 5 节）。
- 多跳 BFS：从种子（影片或人物）出发，按「角色权重 / 跳数衰减」做非负权重最短路
  （类 Dijkstra），收集到达的影片节点，按「路径强度 × 质量偏置」排序，并回溯最佳路径
  生成人类可读的关系链解释（如「《星际穿越》→ 导演 诺兰 → 《盗梦空间》」）。
"""
import logging
from typing import Optional

from graph.schema import (
    REL_LABEL_CN,
    REL_WEIGHT,
    ROLE_LABEL_CN,
    ROLE_TO_REL,
    Relation,
    parse_entity_kind,
    person_key,
    genre_key,
    country_key,
    ENTITY_MOVIE,
    ENTITY_PERSON,
    ENTITY_GENRE,
    ENTITY_COUNTRY,
)
from retrieval.candidates import Candidate, split_genres

logger = logging.getLogger("movie_agent.graph.store")


class MysqlGraphFetcher:
    """基于现有 MySQL 关系表的 GraphFetcher 实现（开发/生产通用）。"""

    def __init__(self, cache=None):
        from db.session import fetch_all
        self._fetch = fetch_all

    # ---- 工具 ----
    @staticmethod
    def _src_of(key: str) -> str:
        return "douban" if key.startswith("douban:") else "imdb"

    def _subject_id(self, movie_key: str):
        """影片 key → 关系表用的 subject id（douban_id / imdb_id）。"""
        src = self._src_of(movie_key)
        if src == "douban":
            pk = int(movie_key.split(":", 1)[1])
            rows = self._fetch(
                "SELECT douban_id FROM douban_main WHERE id=:pk", {"pk": pk}
            )
            return src, (rows[0]["douban_id"] if rows else None)
        imdb_id = movie_key.split(":", 1)[1]
        return src, imdb_id

    def _movie_key_from_subject(self, src: str, subj_id) -> Optional[str]:
        """关系表 subject id → 影片 key（douban:{PK} / imdb:{imdb_id}）。"""
        if src == "douban":
            rows = self._fetch(
                "SELECT id FROM douban_main WHERE douban_id=:d LIMIT 1", {"d": subj_id}
            )
            return f"douban:{rows[0]['id']}" if rows else None
        return f"imdb:{subj_id}"

    # ---- GraphFetcher 接口 ----
    def relations_from(self, key: str) -> list[Relation]:
        kind = parse_entity_kind(key)
        if kind == ENTITY_MOVIE:
            return self._movie_relations(key)
        if kind == ENTITY_PERSON:
            return self._person_relations(key)
        if kind == ENTITY_GENRE:
            return self._attr_relations(key, "genre")
        if kind == ENTITY_COUNTRY:
            return self._attr_relations(key, "country")
        return []

    def _movie_relations(self, key: str) -> list[Relation]:
        src, subj = self._subject_id(key)
        if subj is None:
            return []
        rels: list[Relation] = []
        # 1) 人物关系（导演/编剧/卡司）
        rows = self._fetch(
            f"SELECT name_id, credits_type FROM {src}_name_relate WHERE "
            f"{'douban_id' if src=='douban' else 'imdb_id'}=:s",
            {"s": subj},
        )
        for r in rows:
            role = r["credits_type"] or "cast"
            rel_type, w = ROLE_TO_REL.get(role, ("acted_by", 1.0))
            rels.append(Relation(
                subj=key, pred=rel_type, obj=person_key(src, r["name_id"]),
                weight=w, role=role,
            ))
        # 2) 类型 / 地区属性边
        main = self._fetch(
            f"SELECT genres, country FROM {src}_main WHERE "
            f"{'id' if src=='douban' else 'imdb_id'}=:s LIMIT 1",
            {"s": subj},
        )
        if main:
            g = (main[0]["genres"] or "")
            for tok in [x for x in g.replace("|", "/").split("/") if x.strip()]:
                rels.append(Relation(subj=key, pred="has_genre",
                                     obj=genre_key(tok.strip()), weight=REL_WEIGHT["has_genre"]))
            c = (main[0]["country"] or "")
            for tok in [x for x in c.replace("|", "/").split("/") if x.strip()]:
                rels.append(Relation(subj=key, pred="from_country",
                                     obj=country_key(tok.strip()), weight=REL_WEIGHT["from_country"]))
        return rels

    def _person_relations(self, key: str) -> list[Relation]:
        src = self._src_of(key)
        name_id = key.split(":", 1)[1]
        idcol = "douban_id" if src == "douban" else "imdb_id"
        rows = self._fetch(
            f"SELECT {idcol} AS sid, credits_type FROM {src}_name_relate "
            f"WHERE name_id=:nid LIMIT 60",
            {"nid": name_id},
        )
        rels: list[Relation] = []
        for r in rows:
            mk = self._movie_key_from_subject(src, r["sid"])
            if not mk:
                continue
            role = r["credits_type"] or "cast"
            rel_type, w = ROLE_TO_REL.get(role, ("acted_by", 1.0))
            rels.append(Relation(
                subj=key, pred=rel_type, obj=mk, weight=w, role=role,
            ))
        return rels

    def _attr_relations(self, key: str, attr: str) -> list[Relation]:
        """类型/地区 → 影片（反向边，限制数量控成本）。

        P2-4：类型扩展改走维度关联表（{src}_genre_relate + {src}_genre）——
        - 旧实现对 douban_main/imdb_main 的 `genres LIKE '%name%'` 每类型两表
          各全表扫描一次，全量灌入后延迟显著；关联表按 (genre_id, 影片id) 建索引，
          等值 JOIN 成本恒定；
        - 且消除子串误匹配（LIKE '%动画%' 会把「动画电影」也扩进来）。
        维度表缺失（开发库未建）或查询失败时自动回退旧 LIKE 路径，图谱不塌。
        地区维度暂维持 LIKE：dim 名称口径（country_short_name）与 main.country
        管道串（可能含「中国大陆」等全称）不完全一致，等值匹配会丢边。
        """
        name = key.split(":", 1)[1]
        pred = "has_genre" if attr == "genre" else "from_country"
        w = REL_WEIGHT[pred]
        rels: list[Relation] = []
        for src in ("douban", "imdb"):
            if attr == "genre":
                rels.extend(self._genre_relate_relations(src, key, name, pred, w))
                continue
            col = "country"
            idcol = "id" if src == "douban" else "imdb_id"
            rows = self._fetch(
                f"SELECT {idcol} AS sid, rating, rate_agg FROM {src}_main "
                f"WHERE {col} LIKE :pat ORDER BY "
                f"{'rating' if src=='douban' else 'rate_agg'} DESC LIMIT 40",
                {"pat": f"%{name}%"},
            )
            for r in rows:
                mk = self._movie_key_from_subject(src, r["sid"])
                if mk:
                    rels.append(Relation(subj=key, pred=pred, obj=mk, weight=w))
        return rels

    def _genre_relate_relations(
        self, src: str, key: str, name: str, pred: str, w: float
    ) -> list[Relation]:
        """类型 → 影片：{src}_genre_relate 等值 JOIN（P2-4），失败回退 LIKE。

        影片 key 直接由 JOIN 出的主键构造，不经 _movie_key_from_subject 的逐行回查
        （旧路径每行一次 SELECT id FROM douban_main，40 行 = 40 次往返）。
        """
        relate, dim = f"{src}_genre_relate", f"{src}_genre"
        keycol = "douban_id" if src == "douban" else "imdb_id"
        maincol = "rating" if src == "douban" else "rate_agg"
        rows = None
        try:
            rows = self._fetch(
                f"SELECT m.id AS pk, m.imdb_id AS iid FROM {relate} g "
                f"JOIN {dim} d ON d.id = g.genre_id "
                f"JOIN {src}_main m ON m.{keycol} = g.{keycol} "
                f"WHERE d.genre_name = :name OR d.genre_cn_name = :name "
                f"ORDER BY m.{maincol} DESC LIMIT 40",
                {"name": name},
            )
        except Exception as e:  # noqa: BLE001 - 维度表缺失/列不存在 → 回退 LIKE
            logger.warning("类型维度关联表查询失败，回退 LIKE 路径（%s）：%s", relate, e)
            rows = self._fetch(
                f"SELECT id AS pk, imdb_id AS iid FROM {src}_main "
                f"WHERE genres LIKE :pat ORDER BY {maincol} DESC LIMIT 40",
                {"pat": f"%{name}%"},
            )
        out: list[Relation] = []
        for r in rows or []:
            mk = f"douban:{r['pk']}" if src == "douban" else (
                f"imdb:{r['iid']}" if r.get("iid") else None)
            if mk:
                out.append(Relation(subj=key, pred=pred, obj=mk, weight=w))
        return out

    def movie_meta(self, item_ids: list[str]) -> dict:
        """批量取影片元数据，按 item_id 返回。"""
        out: dict = {}
        douban_pks, imdb_ids = [], []
        for iid in item_ids:
            if iid.startswith("douban:"):
                douban_pks.append(int(iid.split(":", 1)[1]))
            elif iid.startswith("imdb:"):
                imdb_ids.append(iid.split(":", 1)[1])
        if douban_pks:
            rows = self._fetch(
                "SELECT id, douban_id, imdb_id, title_text_cn, original_title, "
                "release_date_year, rating, rating_count, genres, country "
                "FROM douban_main WHERE id IN :pks",
                {"pks": douban_pks},
            )
            for m in rows:
                out[f"douban:{m['id']}"] = {
                    "source": "douban",
                    "title": (m["title_text_cn"] or m["original_title"] or "").strip(),
                    "year": int(m["release_date_year"] or 0),
                    "rating": float(m["rating"] or 0),
                    "rating_count": int(m["rating_count"] or 0),
                    "genres": m["genres"] or "",
                    "country": m["country"] or "",
                    "douban_id": str(m.get("douban_id") or ""),
                    # P1-6：豆瓣候选必须带 imdb_id——work_key 优先以 imdb_id 归并
                    # 双源同片，缺它则 relation/graph/person 产出的豆瓣候选与
                    # content 召回的同片各成一个 work，结果列表出现重复影片，
                    # 且曝光/反馈按不同 id 双计。
                    "imdb_id": str(m.get("imdb_id") or ""),
                }
        if imdb_ids:
            rows = self._fetch(
                "SELECT imdb_id, title_text_cn, title_text, release_date_year, "
                "rate_agg, rate_votes, genres, country FROM imdb_main "
                "WHERE imdb_id IN :ids",
                {"ids": imdb_ids},
            )
            for m in rows:
                out[f"imdb:{m['imdb_id']}"] = {
                    "source": "imdb",
                    "title": (m["title_text_cn"] or m["title_text"] or "").strip(),
                    "year": int(m["release_date_year"] or 0),
                    "rating": float(m["rate_agg"] or 0),
                    "rating_count": int(m["rate_votes"] or 0),
                    "genres": m["genres"] or "",
                    "country": m["country"] or "",
                    "imdb_id": str(m.get("imdb_id") or ""),
                }
        return out

    def person_label(self, person_key_: str) -> str:
        src = self._src_of(person_key_)
        name_id = person_key_.split(":", 1)[1]
        rows = self._fetch(
            f"SELECT name, name_cn, name_en FROM {src}_name WHERE name_id=:nid LIMIT 1",
            {"nid": name_id},
        )
        if not rows:
            return ""
        r = rows[0]
        return (r.get("name_cn") or r.get("name") or r.get("name_en") or "").strip()

    def search_person(self, name: str, limit: int = 5) -> list[str]:
        pat = f"%{name}%"
        keys: list[str] = []
        for src in ("douban", "imdb"):
            rows = self._fetch(
                f"SELECT name_id FROM {src}_name WHERE name LIKE :pat "
                f"OR name_cn LIKE :pat OR name_en LIKE :pat LIMIT :lim",
                {"pat": pat, "lim": limit},
            )
            for r in rows:
                keys.append(person_key(src, r["name_id"]))
        return keys


class KnowledgeGraphStore:
    """邻接缓存 + 多跳 BFS 遍历。生产可换 Neo4j（接口不变）。"""

    def __init__(self, fetcher, cache_max: int = 20000):
        self._f = fetcher
        self._cache: dict[str, list[Relation]] = {}
        self._cache_max = cache_max

    def search_person(self, name: str, limit: int = 5) -> list[str]:
        """按人名检索人物 key（实体中心召回入口）。"""
        return self._f.search_person(name, limit=limit)

    def person_filmography(self, name: str, top_k: int = 20) -> list:
        """按人名取「其本人作品集」（导演/编剧/卡司直接署名的影片），返回 GraphHit 列表。

        与 graph_recall_by_entity 的区别：不做多跳合作网络扩展，只返回该人署名的作品，
        更贴合「X 导演/演员的作品」这类意图；且**无 GRAPH_ENABLED 依赖**（仅用 fetcher，
        即便图谱遍历被关闭也能返回人物作品集）。
        """
        from graph.schema import GraphHit

        if not name or not name.strip():
            return []
        pkeys = self._f.search_person(name.strip(), limit=5)
        if not pkeys:
            return []
        item_ids: list[str] = []
        seen: set[str] = set()
        for pk in pkeys:
            # 走公开 GraphFetcher 接口 relations_from（经 _neighbors 缓存）：
            # 曾误写 self._person_relations(pk)——那是 MysqlGraphFetcher 的私有
            # 方法，KnowledgeGraphStore 上不存在，person_recall 必抛
            # AttributeError 被 runner 吞掉返回空，by_person 整条链路静默失效。
            for rel in self._neighbors(pk):
                obj = rel.obj
                if (obj.startswith("douban:") or obj.startswith("imdb:")) and obj not in seen:
                    seen.add(obj)
                    item_ids.append(obj)
        if not item_ids:
            return []
        meta = self._f.movie_meta(item_ids)
        hits: list = []
        for iid in item_ids:
            m = meta.get(iid)
            if not m or not m.get("title"):
                continue
            score = 0.6 + 0.4 * min(max(m["rating"], 0), 10) / 10.0
            hits.append(GraphHit(
                item_id=iid, source=m["source"], title=m["title"], year=m["year"],
                genres=[g for g in m["genres"].replace("|", "/").split("/") if g.strip()],
                country=m["country"], rating=m["rating"], rating_count=m["rating_count"],
                score=score, douban_id=m.get("douban_id", "") or "",
                imdb_id=m.get("imdb_id", "") or "",
                paths=[f"人物:{name.strip()} → 署名 → 《{m['title']}》"],
            ))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def _neighbors(self, key: str) -> list[Relation]:
        if key in self._cache:
            return self._cache[key]
        rels = self._f.relations_from(key) or []
        if len(self._cache) >= self._cache_max:
            self._cache.clear()  # 简单 FIFO 淘汰，避免无界增长
        self._cache[key] = rels
        return rels

    def traverse(
        self,
        seeds: list[str],
        max_hops: int = 2,
        top_k: int = 20,
        expand_genre: bool = True,
        expand_country: bool = False,
        quality_bias: bool = True,
    ) -> list:
        """多跳遍历，返回 GraphHit 列表（按综合分降序，已排除种子影片）。

        种子可为影片 key（douban:{PK}/imdb:{imdb_id}）或人物 key
        （douban_name:{id}/imdb_name:{id}）。仅影片节点进入最终结果。
        """
        from graph.schema import GraphHit

        dist: dict[str, float] = {s: 0.0 for s in seeds}
        parent: dict[str, tuple[str, Relation]] = {}
        frontier = list(seeds)
        for hop in range(1, max_hops + 1):
            nxt = []
            for u in frontier:
                for rel in self._neighbors(u):
                    if not expand_genre and rel.pred == "has_genre":
                        continue
                    if not expand_country and rel.pred == "from_country":
                        continue
                    v = rel.obj
                    cand = dist[u] + rel.weight / hop  # 跳数衰减
                    if cand > dist.get(v, -1e9):
                        dist[v] = cand
                        parent[v] = (u, rel)
                        if v not in nxt:
                            nxt.append(v)
            frontier = nxt
            if not frontier:
                break

        movie_keys = [
            k for k in dist
            if parse_entity_kind(k) == ENTITY_MOVIE and k not in seeds
        ]
        if not movie_keys:
            return []
        meta = self._f.movie_meta(movie_keys)
        hits: list[GraphHit] = []
        for k in movie_keys:
            m = meta.get(k)
            if not m or not m.get("title"):
                continue
            score = dist[k]
            if quality_bias:
                rb = 0.6 + 0.4 * min(max(m["rating"], 0), 10) / 10.0
                score *= rb
            hits.append(GraphHit(
                item_id=k, source=m["source"], title=m["title"], year=m["year"],
                genres=[g for g in m["genres"].replace("|", "/").split("/") if g.strip()],
                country=m["country"], rating=m["rating"],
                rating_count=m["rating_count"], score=score,
                douban_id=m.get("douban_id", "") or "",
                imdb_id=m.get("imdb_id", "") or "",
                paths=self._build_paths(k, parent, seeds, meta),
            ))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def _build_paths(self, target: str, parent: dict, seeds: set, meta: dict) -> list[str]:
        """回溯 parent 链，生成人类可读关系链（节点 token 串联）。

        形如「《星际穿越》 → 导演 诺兰 → 《盗梦空间》 → 主演 小李子 → 《泰坦尼克号》」。
        种子为人物时前缀为「人物:诺兰」。
        """
        edges: list[tuple[str, Relation]] = []
        cur = target
        seen = set()
        while cur in parent and cur not in seen:
            seen.add(cur)
            u, rel = parent[cur]
            edges.append((u, rel))
            cur = u
            if cur in seeds:
                break
        if not edges:
            return []
        edges.reverse()
        toks = [self._node_token(edges[0][0], None, meta)]
        for _u, rel in edges:
            toks.append(self._node_token(rel.obj, rel, meta))
        return [" → ".join(toks)]

    def _node_token(self, key: str, rel: Optional[Relation], meta: dict) -> str:
        kind = parse_entity_kind(key)
        if kind == ENTITY_MOVIE:
            title = (meta.get(key) or {}).get("title") or self._label_for(key)
            return f"《{title or '?'}》"
        if kind == ENTITY_PERSON:
            lbl = REL_LABEL_CN.get(rel.pred, "关联") if rel else "人物"
            nm = self._label_for(key)
            return f"{lbl}:{nm}" if nm else lbl
        if kind == ENTITY_GENRE:
            return f"类型:{key.split(':', 1)[1]}"
        if kind == ENTITY_COUNTRY:
            return f"地区:{key.split(':', 1)[1]}"
        return key

    def _label_for(self, key: str) -> str:
        kind = parse_entity_kind(key)
        if kind == ENTITY_MOVIE:
            meta = self._f.movie_meta([key]).get(key)
            return meta["title"] if meta else ""
        if kind == ENTITY_PERSON:
            return self._f.person_label(key)
        if kind in (ENTITY_GENRE, ENTITY_COUNTRY):
            return key.split(":", 1)[1]
        return key


_store_singleton = None


def get_graph_store(cache_max: int = 20000):
    """进程内单例：默认 MySQL fetcher，邻接缓存跨请求复用。"""
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = KnowledgeGraphStore(MysqlGraphFetcher(), cache_max=cache_max)
    return _store_singleton
