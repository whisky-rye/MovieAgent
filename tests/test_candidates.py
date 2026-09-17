"""召回候选数据结构（retrieval/candidates.py）单测。

验证 split_genres 对豆瓣/IMDb 常见的 | / , 、 空格分隔的鲁棒解析，
以及 Candidate dataclass 的字段默认值。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retrieval.candidates import Candidate, split_genres


def test_split_pipe_slash():
    assert split_genres("剧情|科幻/喜剧") == ["剧情", "科幻", "喜剧"]


def test_split_space_and_punct():
    assert split_genres("剧情、动作 爱情") == ["剧情", "动作", "爱情"]


def test_split_empty():
    assert split_genres("") == []
    assert split_genres(None) == []


def test_candidate_defaults():
    c = Candidate(id="douban:1", source="douban", title="X")
    assert c.rating == 0.0 and c.score == 0.0 and c.year == 0 and c.genres == []
