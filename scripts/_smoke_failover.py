"""临时冒烟：验证 FailoverChatModel + 额度熔断。"""
import sys
from langchain_core.exceptions import LangChainException

from config.settings import settings
from llm.client import chat_strong, chat_light
from llm.failover import (
    FailoverChatModel, _is_quota_exhausted, is_model_exhausted,
    reset_exhausted, _EXHAUSTED,
)

print("PROVIDER =", settings.LLM_PROVIDER)
print("APP_ENV =", settings.APP_ENV)
print("STRONG_CHAIN =", settings.cloud_strong_chain)
print("LIGHT_CHAIN  =", settings.cloud_light_chain)
print("LOCAL_FALLBACK =", settings.CLOUD_LLM_LOCAL_FALLBACK)
print("MAX_FALLBACKS =", settings.CLOUD_LLM_MAX_FALLBACKS)

# 1) 构造：云端模式应返回 FailoverChatModel，且强/弱档各自取对应链
s = chat_strong()
l = chat_light()
print("strong type =", type(s).__name__)
print("light  type =", type(l).__name__)
assert isinstance(s, FailoverChatModel)
assert isinstance(l, FailoverChatModel)
assert s.models == settings.cloud_strong_chain
assert l.models == settings.cloud_light_chain
assert s.local_fallback_model == settings.CLOUD_LLM_LOCAL_FALLBACK

# 2) 额度判定：伪造 403 AllocationQuota.FreeTierOnly
class FakeErr(Exception):
    status_code = 403
fake = FakeErr("openai types: AllocationQuota.FreeTierOnly. free tier exhausted")
print("is_quota_exhausted(fake403) =", _is_quota_exhausted(fake))
assert _is_quota_exhausted(fake) is True
# 普通错误不应判为额度耗尽
print("is_quota_exhausted(ValueError) =", _is_quota_exhausted(ValueError("timeout")))
assert _is_quota_exhausted(ValueError("timeout")) is False

# 3) 熔断持久化 / 查询 / 重置（隔离：用完还原）
_saved = set(_EXHAUSTED)
_EXHAUSTED.add("glm-5.2")
print("is_model_exhausted(glm-5.2) after add =", is_model_exhausted("glm-5.2"))
assert is_model_exhausted("glm-5.2") is True
reset_exhausted()
print("is_model_exhausted(glm-5.2) after reset =", is_model_exhausted("glm-5.2"))
assert is_model_exhausted("glm-5.2") is False
_EXHAUSTED.update(_saved)  # 还原现场

# 4) 真实调用：第一个模型 qwen3.8-27b 应走通（并写入 log/model_failover.log）
print("--- real invoke via FailoverChatModel ---")
out = chat_strong().invoke("用一句话回答：1+1 等于几？")
print("REAL_OUT =", str(getattr(out, "content", out))[:200])
print("ALL_SMOKE_OK")
