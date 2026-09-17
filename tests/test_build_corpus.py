"""ETL 语料构建（etl/build_corpus.py）单测。

验证纯函数（不依赖 DB）：
- _clean 折叠空白、空值安全；
- _to_int / _to_float 容错解析；
- _split_genres 与召回层一致的分隔；
- _clean_meta 移除空 genres_list（Chroma 不允许空 list metadata）；
- _row_to_douban_rec / _row_to_imdb_rec 拼装 doc 与 meta（含 IMDb 用 rate_agg/rate_votes）；
- 标题缺失返回 None。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from etl import build_corpus as bc


def test_clean():
    assert bc._clean("  a\n b \t c ") == "a b c"
    assert bc._clean(None) == ""


def test_to_int_float():
    assert bc._to_int("12") == 12
    assert bc._to_int("x") == 0
    assert bc._to_float("8.5") == 8.5
    assert bc._to_float(None) == 0.0


def test_split_genres_equiv():
    assert bc._split_genres("剧情|科幻/喜剧") == ["剧情", "科幻", "喜剧"]


def test_clean_meta_drops_empty_list():
    out = bc._clean_meta({"a": 1, "genres_list": []})
    assert "genres_list" not in out and out["a"] == 1


def test_row_to_douban_rec():
    row = {
        "title_text_cn": "盗梦空间", "original_title": "Inception", "release_date_year": 2010,
        "genres": "科幻|悬疑", "country": "美国", "director_name": "诺兰",
        "description": "x", "plot": "y", "tags": "梦", "rating": 9.4,
        "rating_count": 100000, "id": 5,
    }
    rec = bc._row_to_douban_rec(row)
    assert rec["id"] == "douban:5" and rec["source"] == "douban"
    assert "盗梦空间" in rec["doc"] and rec["meta"]["year"] == 2010
    assert rec["meta"]["genres_list"] == ["科幻", "悬疑"]


def test_row_to_imdb_rec_uses_rate_agg():
    row = {
        "imdb_id": "tt123", "title_text_cn": "X", "release_date_year": 2011, "genres": "Drama",
        "country": "US", "director_name": "d", "rate_agg": 8.1, "rate_votes": 5000,
        "plot_text": "p", "plot_keywords": "k", "tagline": "t",
    }
    rec = bc._row_to_imdb_rec(row)
    assert rec["id"] == "imdb:tt123"
    assert rec["meta"]["rating"] == 8.1 and rec["meta"]["rating_count"] == 5000


def test_row_to_douban_empty_title():
    assert bc._row_to_douban_rec({"title_text_cn": None, "original_title": None}) is None
