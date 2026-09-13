"""P3 验证探针：端到端跑通 Agent 编排（意图→规划→工具→反思→生成）。

运行：PYTHONPATH=. .venv/Scripts/python.exe -u scripts/probe_agent.py
（本地 Ollama 推理，单轮约 10-30s，请耐心）
"""
import sys
import time
from agent.graph import build_agent
from retrieval.candidates import Candidate


def _show(label: str, result: dict):
    print(f"\n===== {label} =====")
    print("intent :", result.get("intent"))
    print("plan   :", result.get("plan"))
    print("ranked :", len(result.get("ranked", [])), "条")
    for i, c in enumerate(result.get("ranked", [])[:5], 1):
        g = "/".join(c.genres[:3]) if c.genres else "-"
        r = f"{c.rating:.1f}" if c.rating else "?"
        print(f"   {i}. {c.title}({c.year}) [{g}] 评分{r} src={c.recall_source} score={c.score:.2f}")
    print("answer :")
    print(result.get("answer", ""))


def main():
    app = build_agent()
    # 场景1：按描述推荐
    t0 = time.time()
    r1 = app.invoke({"user_id": "demo", "query": "想看太空科幻，带点孤独感的作品"})
    print(f"[场景1 耗时 {time.time()-t0:.1f}s]")
    _show("场景1 内容推荐", r1)

    # 场景2：找相似（基于一部作品名）
    t0 = time.time()
    r2 = app.invoke({"user_id": "demo", "query": "有没有类似《机器人总动员》的片子"})
    print(f"[场景2 耗时 {time.time()-t0:.1f}s]")
    _show("场景2 相似召回", r2)

    # 场景3：高分榜单
    t0 = time.time()
    r3 = app.invoke({"user_id": "demo", "query": "来个高分电影榜单"})
    print(f"[场景3 耗时 {time.time()-t0:.1f}s]")
    _show("场景3 高分榜", r3)

    # 场景4：记住偏好
    t0 = time.time()
    r4 = app.invoke({"user_id": "demo", "query": "我比较喜欢科幻和悬疑，记一下"})
    print(f"[场景4 耗时 {time.time()-t0:.1f}s]")
    _show("场景4 偏好记忆", r4)

    # 场景5：偏好生效后的推荐（验证 memory 闭环）
    t0 = time.time()
    r5 = app.invoke({"user_id": "demo", "query": "再给我推几部好看的"})
    print(f"[场景5 耗时 {time.time()-t0:.1f}s]")
    _show("场景5 偏好加权推荐", r5)


if __name__ == "__main__":
    main()
