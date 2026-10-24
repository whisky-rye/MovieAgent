"""P2-5 回归：MMR 选择优化（语义等价）+ ETL 键集分页 + 默认参数函数内解析。

修复前：
- mmr_diversify 每轮 remain.remove(best)（O(n) 删除 × k 轮）+ 每轮重复
  set 构造，top_k 放大后成本平方级增长；
- etl/build_corpus 深 OFFSET 分页（55 万全量建库后半程每页从头扫描）；
- 模块级默认参数 `page_size: int = _DEFAULT_PAGE_SIZE` 导入期绑定，测试
  monkeypatch settings.BUILD_PAGE_SIZE 不生效。

修复后：MMMR 预计算类型集 + 已选标记单遍扫描（选择语义逐位一致，输出可与
旧实现对比验证）；键集分页（WHERE pk > :last ORDER BY pk LIMIT）成本恒定；
page_size 一律函数内读 settings。
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config.settings as cs
import etl.build_corpus as bc
from ranker.scorer import mmr_diversify
from retrieval.candidates import Candidate


# ---------- MMR：语义等价 + 边界 ----------

def _ref_mmr(cands, lambda_div=0.3, top_k=None):
    """旧实现的参考副本（用于逐位对比，验证优化不改变语义）。"""
    if not cands:
        return []
    selected: list[Candidate] = []
    remain = list(cands)
    while remain and (top_k is None or len(selected) < top_k):
        best, best_score = None, -1e9
        for c in remain:
            sim = 0.0
            if c.genres:
                sims = []
                for s in selected:
                    if not s.genres:
                        sims.append(0.0)
                        continue
                    inter = len(set(c.genres) & set(s.genres))
                    uni = len(set(c.genres) | set(s.genres))
                    sims.append(inter / uni if uni else 0.0)
                sim = max(sims) if sims else 0.0
            mmr = (1 - lambda_div) * c.score - lambda_div * sim
            if mmr > best_score:
                best, best_score = c, mmr
        selected.append(best)
        remain.remove(best)
    return selected


def _rand_cands(n, seed):
    rng = random.Random(seed)
    pool = ["剧情", "喜剧", "动作", "科幻", "爱情", "悬疑", "恐怖", "动画"]
    out = []
    for i in range(n):
        k = rng.randint(0, 3)
        out.append(Candidate(
            id=f"douban:{i:03d}", source="douban", title=f"片{i}",
            year=2000, genres=rng.sample(pool, k), score=round(rng.random(), 4),
        ))
    return out


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("top_k", [None, 1, 3, 5, 10, 50])
def test_mmr_equivalent_to_reference(seed, top_k):
    """新旧实现在随机输入 + 各 top_k 下输出逐位一致（语义严格保持）。"""
    cands = _rand_cands(14, seed)
    assert [c.id for c in mmr_diversify(cands, top_k=top_k)] == \
           [c.id for c in _ref_mmr(cands, top_k=top_k)]


def test_mmr_lambda_variants_equivalent():
    cands = _rand_cands(12, 7)
    for lam in (0.0, 0.3, 0.7, 1.0):
        assert [c.id for c in mmr_diversify(cands, lambda_div=lam)] == \
               [c.id for c in _ref_mmr(cands, lambda_div=lam)]


def test_mmr_empty_and_edges():
    assert mmr_diversify([]) == []
    one = _rand_cands(1, 3)
    assert mmr_diversify(one) == one
    assert mmr_diversify(one, top_k=0) == []
    assert len(mmr_diversify(_rand_cands(5, 4), top_k=99)) == 5


def test_mmr_diversifies_same_genre_cluster():
    """同类型簇会被打散到不同名次（多样性目标本身仍成立）。"""
    same = [Candidate(id=f"douban:{i}", source="douban", title=f"片{i}",
                      genres=["科幻"], score=1.0 - i * 0.01) for i in range(6)]
    out = mmr_diversify(same, lambda_div=0.5, top_k=3)
    assert [c.id for c in out] == ["douban:0", "douban:1", "douban:2"]


def test_mmr_deterministic():
    cands = _rand_cands(10, 5)
    assert [c.id for c in mmr_diversify(cands)] == [c.id for c in mmr_diversify(cands)]


# ---------- ETL：键集分页 + 默认参数 ----------

def _douban_row(i):
    return {
        "id": i, "title_text_cn": f"影片{i}", "original_title": None,
        "release_date_year": 2000 + i % 20, "genres": "剧情", "country": "中国",
        "rating": 8.0, "rating_count": 100, "director_name": "导演",
        "description": None, "plot": None, "tags": None,
    }


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeConn:
    def __init__(self, pages, seen):
        self._pages, self._seen = pages, seen

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self._seen.append((str(sql), dict(params or {})))
        page = self._pages.pop(0) if self._pages else []
        return _FakeResult(page)


class _FakeEngine:
    def __init__(self, pages, seen):
        self._pages, self._seen = pages, seen

    def connect(self):
        return _FakeConn(self._pages, self._seen)


@pytest.fixture
def fake_engine(monkeypatch):
    seen = []

    def _factory(dsn):
        return _FakeEngine(fake_engine.pages, seen)

    monkeypatch.setattr(bc, "create_engine", _factory)
    fake_engine.pages = []
    fake_engine.seen = seen
    return fake_engine


def test_iter_douban_uses_keyset_pagination(fake_engine, monkeypatch):
    monkeypatch.setattr(cs.settings, "BUILD_PAGE_SIZE", 2)
    fake_engine.pages = [
        [_douban_row(1), _douban_row(2)],
        [_douban_row(3), _douban_row(4)],
        [_douban_row(5)],
    ]
    pages = list(bc._iter_douban())
    assert [len(p) for p in pages] == [2, 2, 1]
    sqls = [s for s, _ in fake_engine.seen]
    assert all("WHERE id > :last" in s for s in sqls), "必须键集分页"
    assert all("OFFSET" not in s for s in sqls), "不得再用深 OFFSET"
    # last 递进 = 上页最大主键
    lasts = [p["last"] for _, p in fake_engine.seen]
    assert lasts == [0, 2, 4]


def test_iter_douban_page_size_from_settings(fake_engine, monkeypatch):
    """page_size 省略时函数内读 settings（导入期绑定已修复，可 monkeypatch）。"""
    monkeypatch.setattr(cs.settings, "BUILD_PAGE_SIZE", 7)
    fake_engine.pages = [[_douban_row(1)]]
    list(bc._iter_douban())
    assert fake_engine.seen[0][1]["lim"] == 7


def test_iter_imdb_uses_keyset_pagination(fake_engine, monkeypatch):
    monkeypatch.setattr(cs.settings, "BUILD_PAGE_SIZE", 2)

    def _imdb_row(i):
        return {
            "imdb_id": f"tt{i:07d}", "title_text_cn": f"影片{i}", "title_text": None,
            "release_date_year": 2000, "genres": "Action", "country": "USA",
            "director_name": None, "rate_agg": 7.0, "rate_votes": 10,
            "plot_text": None, "plot_keywords": None, "tagline": None,
        }

    fake_engine.pages = [[_imdb_row(1), _imdb_row(2)], [_imdb_row(3)]]
    pages = list(bc._iter_imdb())
    assert [len(p) for p in pages] == [2, 1]
    sqls = [s for s, _ in fake_engine.seen]
    assert all("WHERE imdb_id > :last" in s for s in sqls)
    assert all("OFFSET" not in s for s in sqls)
    lasts = [p["last"] for _, p in fake_engine.seen]
    assert lasts == ["", "tt0000002"]


def test_iter_corpus_chains_both_sources(fake_engine, monkeypatch):
    monkeypatch.setattr(cs.settings, "BUILD_PAGE_SIZE", 5)
    fake_engine.pages = [[_douban_row(1)], []]
    pages = list(bc.iter_corpus())
    assert sum(len(p) for p in pages) == 1
    assert all(p["lim"] == 5 for _, p in fake_engine.seen)
