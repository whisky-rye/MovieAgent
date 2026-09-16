"""P0 回归测试：上下文历史摘要压缩 + 限流后端选择（unittest，无需 pytest）。

仅覆盖纯逻辑/可离线验证的部分：
- build_history_context：短历史全保留、长历史摘要+最近 N 轮原文、缓存复用、摘要失败回退原文；
- RateLimiter：memory 后端令牌桶消耗/限流；redis 后端在 Redis 不可达时回退 memory 不报错。

涉及真实 LLM / 真实 Redis 的部分由集成验证覆盖。
"""
import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import history_compress as hc


def _fake_chat_light(summary_text="SUMMARY_X"):
    captured = {}

    def factory(**kw):
        class _Msg:
            content = summary_text

        def invoke(msgs):
            captured["prompt"] = msgs[0].content
            return _Msg()

        class _LLM:
            def invoke(self, m):
                return invoke(m)

        return _LLM()

    return factory, captured


class HistoryCompressTest(unittest.TestCase):
    def test_short_history_kept_verbatim(self):
        short = [
            {"role": "user", "content": "推荐科幻"},
            {"role": "assistant", "content": "《星际穿越》"},
        ]
        out = hc.build_history_context(short)
        self.assertIn("推荐科幻", out)
        self.assertIn("星际穿越", out)

    def test_long_history_summarized_with_recent_kept(self):
        factory, captured = _fake_chat_light()
        long = [{"role": "user", "content": f"q{i}"} for i in range(10)] + \
               [{"role": "assistant", "content": f"a{i}"} for i in range(10)]
        with mock.patch("llm.client.chat_light", factory):
            out = hc.build_history_context(long, session_id="s1")
        self.assertIn("prompt", captured, "摘要器应被调用")
        self.assertIn("SUMMARY_X", out, "摘要应出现在输出")
        # 最近 KEEP_RECENT 轮（a6..a9）保留原文
        self.assertIn("a9", out)
        self.assertIn("a6", out)

    def test_summary_cache_reused(self):
        factory, captured = _fake_chat_light()
        long = [{"role": "user", "content": f"q{i}"} for i in range(10)] + \
               [{"role": "assistant", "content": f"a{i}"} for i in range(10)]
        with mock.patch("llm.client.chat_light", factory):
            hc.build_history_context(long, session_id="s2")
            captured.clear()
            hc.build_history_context(long, session_id="s2")
        self.assertNotIn("prompt", captured, "相同 session 旧片段未增长时应复用缓存")

    def test_empty_history(self):
        self.assertEqual(hc.build_history_context([]), "")

    def test_summarize_failure_falls_back_to_plain(self):
        def boom(**kw):
            raise RuntimeError("llm down")

        long = [{"role": "user", "content": f"q{i}"} for i in range(10)] + \
               [{"role": "assistant", "content": f"a{i}"} for i in range(10)]
        with mock.patch("llm.client.chat_light", boom):
            out = hc.build_history_context(long, session_id="s3")
        # 回退：原文拼接，至少包含最近轮
        self.assertIn("a9", out)


class RateLimiterTest(unittest.TestCase):
    def _reset_settings(self, backend="memory"):
        import config.settings as s
        s.RATELIMIT_BACKEND = backend
        s.RATELIMIT_RATE = 0.2
        s.RATELIMIT_BURST = 3
        # 重置单例，确保使用新配置
        import api.ratelimit as rl
        rl._limiter = None

    def test_memory_backend_burst_then_limit(self):
        self._reset_settings("memory")
        from api.ratelimit import get_limiter
        lim = get_limiter()
        ok1, _ = lim.consume("u_mem")
        ok2, _ = lim.consume("u_mem")
        ok3, _ = lim.consume("u_mem")
        ok4, wait = lim.consume("u_mem")
        self.assertTrue(ok1 and ok2 and ok3)
        self.assertFalse(ok4)
        self.assertGreater(wait, 0)

    def test_redis_backend_falls_back_when_unavailable(self):
        # Redis 不可达时不应抛错，应自动回退 memory 并允许通过
        self._reset_settings("redis")
        from api.ratelimit import get_limiter
        lim = get_limiter()
        ok, _ = lim.consume("u_redis")
        self.assertTrue(ok, "Redis 不可达应回退 memory 且不阻断请求")


if __name__ == "__main__":
    unittest.main(verbosity=2)
