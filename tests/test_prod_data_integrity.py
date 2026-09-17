"""生产库全链路数据完整性验证（离线，基于 scripts/extract_prod_data.py 生成的 fixtures）。

⚠️ 本测试默认读取 data/prod_imdb_fullchain.json 与 data/prod_douban_fullchain.json。
   若 fixtures 不存在（如 CI 未生成），自动 skip。可先执行：
       python scripts/extract_prod_data.py --limit 2000
   生成后再运行本测试。脚本全程仅 SELECT，绝不对生产库写操作。

覆盖点：
   - 记录数达标（默认 2000/库）
   - 主表主键 + 标题非空
   - 关联表（name/genre/country）外键全部解析成功（0 孤儿 → 记录链路完整）
   - 文本/准备表链路存在
   - IMDb 缺失国家/类别维度表时，从 imdb_main 解析字段兜底（允许少量真实空值）
"""
import json
import os

import pytest

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
IMDB_F = os.path.join(DATA_DIR, "prod_imdb_fullchain.json")
DOUBAN_F = os.path.join(DATA_DIR, "prod_douban_fullchain.json")
EXPECT = int(os.getenv("EXPECT_LIMIT", "2000"))


def _load(path):
    if not os.path.exists(path):
        pytest.skip(f"fixture 缺失: {path}；请先运行 python scripts/extract_prod_data.py")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ----------------------------- IMDb -----------------------------
def test_imdb_count():
    d = _load(IMDB_F)
    assert d["limit"] == EXPECT
    assert len(d["chains"]) == EXPECT


def test_imdb_main_fields():
    d = _load(IMDB_F)
    for ch in d["chains"]:
        m = ch["main"]
        assert m.get("imdb_id"), "imdb_id 为空"
        assert m.get("title_text") or m.get("title_text_cn"), "标题为空"
        assert ch["text"] is not None, "imdb_text 链路缺失"


def test_imdb_name_chain_complete():
    """name_relate 的外键全部能在 imdb_name 维度表解析（0 孤儿 = 链路完整）。"""
    d = _load(IMDB_F)
    total, orphan = 0, 0
    for ch in d["chains"]:
        nm = ch["names"]
        total += len(nm)
        for n in nm:
            assert n.get("name_id") and n.get("name"), "name_relate 字段缺失"
        orphan += sum(1 for v in ch["resolved"].values() if v is None)
    assert total > 0, "未提取到任何 name_relate"
    assert orphan == 0, f"存在 {orphan} 条未解析的 name 外键"


def test_imdb_parsed_country_genre_fallback():
    """IMDb 无独立国家/类别维度表，校验从 imdb_main 解析兜底，允许少量真实空值（<=5%）。"""
    d = _load(IMDB_F)
    n_country_empty = sum(1 for ch in d["chains"] if not ch["parsed"]["country"])
    n_genre_empty = sum(1 for ch in d["chains"] if not ch["parsed"]["genres"])
    assert n_country_empty <= EXPECT * 0.05, f"空国家比例过高: {n_country_empty}"
    assert n_genre_empty <= EXPECT * 0.05, f"空类别比例过高: {n_genre_empty}"


# ----------------------------- Douban -----------------------------
def test_douban_count():
    d = _load(DOUBAN_F)
    assert d["limit"] == EXPECT
    assert len(d["chains"]) == EXPECT


def test_douban_main_fields():
    d = _load(DOUBAN_F)
    for ch in d["chains"]:
        m = ch["main"]
        assert m.get("douban_id"), "douban_id 为空"
        assert m.get("title_text") or m.get("title_text_cn"), "标题为空"
        assert ch["text"] is not None, "douban_text 链路缺失"


def test_douban_genre_country_name_chains_complete():
    """genre_relate / country_relate / name_relate 外键全部解析（0 孤儿）。"""
    d = _load(DOUBAN_F)
    g_o = c_o = n_o = 0
    for ch in d["chains"]:
        g_o += sum(1 for v in ch["resolved"]["genres"].values() if v is None)
        c_o += sum(1 for v in ch["resolved"]["countries"].values() if v is None)
        n_o += sum(1 for v in ch["resolved"]["names"].values() if v is None)
        for x in ch["genres"]:
            assert x.get("genre_id"), "genre_relate 缺 genre_id"
        for x in ch["countries"]:
            assert x.get("country_id"), "country_relate 缺 country_id"
        for x in ch["names"]:
            assert x.get("name_id"), "name_relate 缺 name_id"
    assert g_o == 0, f"genre 外键孤儿 {g_o}"
    assert c_o == 0, f"country 外键孤儿 {c_o}"
    assert n_o == 0, f"name 外键孤儿 {n_o}"


def test_douban_resolved_dim_fields():
    """维度解析结果含有效字段；对真实存在的空值（生产库数据缺口）按容忍度量化而非硬失败。"""
    d = _load(DOUBAN_F)
    TOL = 0.05  # 允许 <=5% 的维度名为空（真实生产数据缺口）
    g_empty = c_empty = n_empty = 0
    g_total = c_total = n_total = 0
    for ch in d["chains"]:
        for v in ch["resolved"]["genres"].values():
            if v:
                g_total += 1
                if not (v.get("genre_name") or v.get("genre_cn_name")):
                    g_empty += 1
        for v in ch["resolved"]["countries"].values():
            if v:
                c_total += 1
                # 真实数据中 country_name 可能为空，country_short_name/country_cn_name 兜底
                if not (v.get("country_name") or v.get("country_short_name")
                        or v.get("country_cn_name")):
                    c_empty += 1
        for v in ch["resolved"]["names"].values():
            if v:
                n_total += 1
                if not (v.get("name") or v.get("name_cn") or v.get("name_en")):
                    n_empty += 1
    assert g_total > 0 and c_total > 0 and n_total > 0, "维度解析未命中任何有效值"
    # 量化空值率（也可作为数据质量基线输出）
    print(f"\n[数据质量] douban 维度空值率: "
          f"genre={g_empty}/{g_total}, country={c_empty}/{c_total}, name={n_empty}/{n_total}")
    assert g_empty <= g_total * TOL, f"genre 空名率超容忍: {g_empty}/{g_total}"
    assert c_empty <= c_total * TOL, f"country 空名率超容忍: {c_empty}/{c_total}"
    assert n_empty <= n_total * TOL, f"name 空名率超容忍: {n_empty}/{n_total}"
