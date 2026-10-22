"""进程内指标注册表（线程安全，零依赖）。

提供三类基础指标：
- Counter：单调递增计数（请求数、错误数、token 消耗、护栏拦截数…），支持 tags（维度）。
- Gauge  ：瞬时值（当前并发、存储健康、缓存命中率…）。
- Histogram：采样分布（延迟、token 数…），自动算 count/sum/分位(p50/p95/p99)。

对外暴露两个聚合视图：
- snapshot()      ：结构化 dict（看板/JSON 用）。
- to_prometheus() ：Prometheus 文本格式（GET /metrics 用）。

使用约定：埋点处统一调用 metrics.incr / metrics.observe / metrics.set，
指标名用英文点号命名（如 http_requests_total、llm_call_duration_ms）。
"""
from __future__ import annotations

import threading
from collections import deque


class _Counter:
    __slots__ = ("_lock", "_val")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._val = 0

    def inc(self, n: float = 1) -> None:
        with self._lock:
            self._val += n

    def get(self) -> float:
        with self._lock:
            return self._val


class _Gauge:
    __slots__ = ("_lock", "_val")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._val = 0.0

    def set(self, v: float) -> None:
        with self._lock:
            self._val = v

    def get(self) -> float:
        with self._lock:
            return self._val


class _Histogram:
    __slots__ = ("_lock", "_samples")

    def __init__(self, maxlen: int = 2000) -> None:
        self._lock = threading.Lock()
        self._samples: deque = deque(maxlen=maxlen)

    def observe(self, v: float) -> None:
        with self._lock:
            self._samples.append(v)

    def stats(self) -> dict:
        with self._lock:
            s = list(self._samples)
        if not s:
            return {"count": 0, "sum": 0, "min": 0, "max": 0,
                    "avg": 0, "p50": 0, "p95": 0, "p99": 0}
        s2 = sorted(s)
        n = len(s2)

        def _pct(p: float) -> float:
            idx = min(n - 1, int(p * n))
            return s2[idx]

        return {
            "count": n,
            "sum": sum(s2),
            "min": s2[0],
            "max": s2[-1],
            "avg": sum(s2) / n,
            "p50": _pct(0.50),
            "p95": _pct(0.95),
            "p99": _pct(0.99),
        }


def _tag_key(tags: dict | None):
    if not tags:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in tags.items()))


def _prom_labels(tags: dict | None) -> str:
    if not tags:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(tags.items()))
    return "{" + inner + "}"


class Metrics:
    """线程安全指标注册表（全局单例 metrics）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict = {}
        self._gauges: dict = {}
        self._histos: dict = {}
        self.enabled = True  # 由 settings.METRICS_ENABLED 控制（零开销关闭埋点）

    def set_enabled(self, v: bool) -> None:
        self.enabled = bool(v)

    # —— 写入接口 ——
    def incr(self, name: str, n: float = 1, tags: dict | None = None) -> None:
        if not self.enabled or n == 0:
            return
        with self._lock:
            k = (name, _tag_key(tags))
            c = self._counters.get(k)
            if c is None:
                c = _Counter()
                self._counters[k] = c
        c.inc(n)

    def set(self, name: str, v: float, tags: dict | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            k = (name, _tag_key(tags))
            g = self._gauges.get(k)
            if g is None:
                g = _Gauge()
                self._gauges[k] = g
        g.set(v)

    def observe(self, name: str, v: float, tags: dict | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            k = (name, _tag_key(tags))
            h = self._histos.get(k)
            if h is None:
                h = _Histogram()
                self._histos[k] = h
        h.observe(v)

    # —— 读取接口 ——
    def snapshot(self) -> dict:
        out = {"counters": {}, "gauges": {}, "histograms": {}}
        with self._lock:
            for (name, tags), c in self._counters.items():
                out["counters"].setdefault(name, []).append(
                    {"tags": dict(tags), "value": c.get()})
            for (name, tags), g in self._gauges.items():
                out["gauges"].setdefault(name, []).append(
                    {"tags": dict(tags), "value": g.get()})
            for (name, tags), h in self._histos.items():
                st = h.stats()
                st["tags"] = dict(tags)
                out["histograms"].setdefault(name, []).append(st)
        return out

    def to_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, tags), c in self._counters.items():
                lines.append(f"# TYPE {name} counter")
                lines.append(f"{name}{_prom_labels(dict(tags))} {c.get()}")
            for (name, tags), g in self._gauges.items():
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name}{_prom_labels(dict(tags))} {g.get()}")
            for (name, tags), h in self._histos.items():
                st = h.stats()
                if st["count"] == 0:
                    continue
                lines.append(f"# TYPE {name} histogram")
                lines.append(f"{name}_count{_prom_labels(dict(tags))} {st['count']}")
                lines.append(f"{name}_sum{_prom_labels(dict(tags))} {st['sum']}")
                lines.append(f"{name}_p95{_prom_labels(dict(tags))} {st['p95']}")
        return "\n".join(lines) + "\n"


# 全局单例
metrics = Metrics()
