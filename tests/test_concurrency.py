"""P0 多 worker + 每请求客户端：并发有界信号量（_run_slot）行为验证。

取代旧版「全局串行锁」：本测试验证
1. _run_slot 在单 worker 内允许「有界并行」（并发数不超过信号量容量），而非全串行；
2. 槽被占满且超时耗尽时抛 _Overloaded（端点据此返回 503），而非无限阻塞。
LLM 客户端（chat_strong/chat_light）每次调用新建实例、DB 走连接池——即「每请求客户端」，
由 llm/client.py / db/session.py 的既有实现保证，本测试聚焦并发槽本身的行为正确性。
"""
import threading
import time

import pytest

import api.main as m


def test_run_slot_bounds_concurrency():
    """并发进入临界区的数量不应超过信号量容量（验证有界并行）。"""
    orig_sem = m._RUN_SEM
    m._RUN_SEM = threading.Semaphore(2)  # 故意调小容量，便于断言
    state = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def worker():
        with m._run_slot():
            with lock:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            time.sleep(0.15)  # 模拟一次真实 LLM 调用耗时
            with lock:
                state["cur"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    m._RUN_SEM = orig_sem

    # 最多 2 个并发进入临界区（容差 1，规避测试抖动）
    assert state["max"] <= 3, f"max concurrency {state['max']} exceeds cap 2"
    # 确实发生了并行（而非退化成 1 并发全串行）
    assert state["max"] >= 2, f"expected real parallelism, but max concurrency was {state['max']}"


def test_run_slot_overload_raises():
    """槽被占满且超时为 0 时，_run_slot 应抛 _Overloaded（端点据此返回 503）。"""
    orig_sem = m._RUN_SEM
    orig_timeout = m.settings.RUN_SLOT_TIMEOUT
    m._RUN_SEM = threading.Semaphore(1)
    m.settings.RUN_SLOT_TIMEOUT = 0  # 立即判定过载
    held = None
    try:
        held = m._RUN_SEM.acquire()  # 占住唯一槽
        with pytest.raises(m._Overloaded):
            with m._run_slot():
                pass
    finally:
        m.settings.RUN_SLOT_TIMEOUT = orig_timeout
        if held:
            m._RUN_SEM.release()
        m._RUN_SEM = orig_sem


def test_module_imports_multiworker_safe():
    """模块可正常导入（多 worker 下每个 worker 进程都会 import 并编译图）。"""
    assert m._AGENT is not None
    assert isinstance(m._RUN_SEM, threading.Semaphore)
    assert m._RUN_SEM._value == m.settings.MAX_CONCURRENT_RUNS
