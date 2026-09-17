"""测试公共 fixture。

强 MySQL 约定：业务存储（users / sessions / shared_sessions / user_preferences）统一落
movie_agent 库，MySQL 不可达则服务拒绝启动，无 file 兜底。因此依赖 MySQL 的测试在
MySQL 不可用时自动 skip（而非报错），保证 CI / 离线环境下 `pytest tests` 仍可干净退出。

如需本地运行全部测试，请确保：
  - MySQL 在线，且已执行 scripts/init_movie_agent_db.py 初始化业务库。
"""
import os
import sys

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings  # noqa: E402


@pytest.fixture(scope="session")
def mysql_ok():
    """MySQL 可达性探测；不可达则整个会话的存储测试 skip。"""
    try:
        engine = create_engine(settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True)
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MySQL 不可用，跳过依赖 MySQL 的测试：{exc}")
    return True
