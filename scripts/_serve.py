"""P4.3 进程脱离启动器：在 Windows 下以 DETACHED_PROCESS 拉起 uvicorn，
使子进程不依赖启动它的 shell（避免 Git Bash 无 nohup/setsid 且每次 Bash 调用都是新 shell 导致后台进程被回收）。

- 用法（内部，由 dev.sh 调用）：`python scripts/_serve.py`
- 仅负责 spawn + 落日志 + 打印 pid；启动后立刻退出，uvicorn 子进程独立存活。
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "log", "dev_uvicorn.log")
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

# Windows 进程脱离标志：DETACHED_PROCESS(0x8) | CREATE_NEW_PROCESS_GROUP(0x200)
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def main() -> int:
    if not os.path.exists(PY):
        print(f"[_serve] 未找到 venv python: {PY}", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT

    with open(LOG_PATH, "ab", buffering=1) as logf:
        proc = subprocess.Popen(
            [PY, "-u", "-m", "uvicorn", "api.main:app",
             "--host", "127.0.0.1", "--port", "8000"],
            cwd=ROOT,
            env=env,
            stdout=logf,
            stderr=logf,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        )
    print(proc.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
