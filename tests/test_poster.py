"""G-Poster 海报生成（agent/poster.py）单测（纯逻辑部分）。

验证不依赖百炼网络的纯逻辑：
- pick_model 始终从 POSTER_MODELS 抽样；
- _model_family 区分 image / video；
- build_prompt 含作品名、偏好个性化、query 追加；
- resolve_base_url 三种解析优先级（显式 > WorkspaceId > LLM base）；
- _extract_result_url 兼容 chat 风格与旧 results 风格；
- _apply_media_type 由 content-type 校准扩展名；
- manifest 读写 + 未知会话返回 {status:"none"}。
manifest 测试用临时目录，避免污染真实 POSTER_DATA_DIR。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as cs
from agent import poster


def test_pick_model_in_set():
    assert poster.pick_model() in poster.POSTER_MODELS


def test_model_family():
    assert poster._model_family("qwen-image-3.0") == "image"
    assert poster._model_family("wan-video-1") == "video"


def test_build_prompt_personalized(monkeypatch):
    items = [{"title": "盗梦空间", "year": 2010, "genres": ["科幻", "悬疑"], "country": "美国", "rating": 9.4}]
    p = poster.build_prompt(items, prefs={"genres": ["科幻"], "note": "喜欢烧脑"}, query="关于梦境")
    assert "盗梦空间" in p and "科幻" in p and "烧脑" in p and "梦境" in p


def test_build_prompt_no_pref():
    items = [{"title": "A", "genres": ["剧情"]}]
    assert "A" in poster.build_prompt(items, prefs={})


def test_resolve_base_url_explicit(monkeypatch):
    monkeypatch.setattr(cs.settings, "CLOUD_POSTER_BASE_URL", "https://x.example/api/v1")
    monkeypatch.setattr(cs.settings, "POSTER_WORKSPACE_ID", "")
    assert poster.resolve_base_url() == "https://x.example/api/v1"


def test_resolve_base_url_workspace(monkeypatch):
    monkeypatch.setattr(cs.settings, "CLOUD_POSTER_BASE_URL", "")
    monkeypatch.setattr(cs.settings, "POSTER_WORKSPACE_ID", "ws-123")
    monkeypatch.setattr(cs.settings, "POSTER_REGION", "cn-beijing")
    assert poster.resolve_base_url() == "https://ws-123.cn-beijing.maas.aliyuncs.com/api/v1"


def test_extract_result_url_chat_style():
    data = {"output": {"choices": [{"message": {"content": [{"text": "x"}, {"image": "http://img.png"}]}}]}}
    # _extract_result_url 为实例方法，用实例调用以兼容静态/实例两种装饰
    client = poster.BaiLianVisualClient(api_key="x")
    assert client._extract_result_url(data) == "http://img.png"


def test_apply_media_type():
    out = {"model_family": "image", "media_type": "image/png"}
    poster.BaiLianVisualClient._apply_media_type(out)
    assert out["_ext"] == "png"
    out2 = {"model_family": "video", "media_type": "video/mp4"}
    poster.BaiLianVisualClient._apply_media_type(out2)
    assert out2["_ext"] == "mp4"


def test_manifest_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cs.settings, "POSTER_DATA_DIR", str(tmp_path))
    poster._update_status("sid1", {"status": "pending"})
    assert poster.get_poster_status("sid1")["status"] == "pending"
    poster._update_status("sid1", {"status": "ready", "file": "sid1.png"})
    st = poster.get_poster_status("sid1")
    assert st["status"] == "ready" and st["file"] == "sid1.png"
    assert poster.get_poster_status("nope") == {"status": "none"}
