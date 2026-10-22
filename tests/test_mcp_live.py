"""🔴 真实远端**只读**集成测试（live）。

⚠️ 本文件**只允许 SELECT**：
- 写操作的断言在**护栏层**完成（断言"被拒绝且连接从未被建立"），
  **绝不**向远端发送任何写语句；
- 远端不可达 / 未配置凭据时自动 skip（离线环境干净跳过）。

前置：.env 或环境变量中配置 MCP_DB_HOST / MCP_DB_USER / MCP_DB_PASSWORD。
"""
import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings  # noqa: E402
from mcp_prod import queries as Q  # noqa: E402
from mcp_prod import schema_map as sm  # noqa: E402
from mcp_prod.connection import (  # noqa: E402
    McpReadOnlyViolation,
    get_prod_client,
    reset_prod_client,
)
from mcp_prod.guard import ReadOnlyViolation  # noqa: E402


def _remote_alive() -> bool:
    host = settings.MCP_DB_HOST
    if not host:
        return False
    try:
        s = socket.create_connection((host, int(settings.MCP_DB_PORT or 3306)), timeout=5)
        s.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _configured() -> bool:
    return bool(settings.MCP_DB_HOST and settings.MCP_DB_USER and settings.MCP_DB_PASSWORD)


pytestmark = pytest.mark.skipif(
    not (_remote_alive() and _configured()),
    reason="远端生产库不可达或未配置只读凭据（离线跳过）",
)


@pytest.fixture(scope="module")
def client():
    """真实远端只读客户端。

    限流额度在本测试内放宽（受控的测试流量），限流行为本身由
    tests/test_mcp_connection.py 单测覆盖，避免测试之间互相干扰。
    """
    import types

    reset_prod_client()
    s = types.SimpleNamespace(**{**vars(settings)})
    s.MCP_RATE_LIMIT_QPM = 600
    s.MCP_RATE_LIMIT_BURST = 120
    from mcp_prod.connection import ProdReadOnlyClient

    c = ProdReadOnlyClient(settings_obj=s)
    yield c
    c.close()
    reset_prod_client()


# ---------------------------------------------------------------------------
# 🔴 红线：写操作必须在护栏层被拒，且连接从未被建立
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "DELETE FROM douban_main WHERE id = 1",
    "UPDATE douban_main SET title_text_cn = 'x' WHERE id = 1",
    "INSERT INTO douban_main (id) VALUES (999999999)",
    "DROP TABLE douban_main",
    "TRUNCATE TABLE douban_main",
    "ALTER TABLE douban_main ADD COLUMN zzz INT",
    "CREATE TABLE zzz_probe (id INT)",
    "GRANT ALL ON *.* TO 'probe'@'%'",
])
def test_write_blocked_before_touching_remote(sql, client):
    """🔴 核心：写操作被护栏拦下，远端**一条语句都没收到**。"""
    with pytest.raises(McpReadOnlyViolation):
        client.query(client.db_for_source("douban"), sql, tool="live_write_probe")
    # 断言到达：说明是护栏抛的（而非数据库报错）


def test_cross_schema_read_blocked(client):
    with pytest.raises(McpReadOnlyViolation):
        client.query("imdb2", "SELECT * FROM mysql.user", tool="live_mysql_probe")


def test_information_schema_blocked_by_default(client):
    with pytest.raises(McpReadOnlyViolation):
        client.query("imdb2", "SELECT * FROM information_schema.tables",
                     tool="live_is_probe")


def test_multistatement_blocked(client):
    with pytest.raises(McpReadOnlyViolation):
        client.query("imdb2", "SELECT 1; SELECT 2", tool="live_multi_probe")


# ---------------------------------------------------------------------------
# 只读能力可用性
# ---------------------------------------------------------------------------

def test_search_movie_returns_normalized_items(client):
    out = Q.search_movie("肖申克", source="douban", limit=3, client=client)
    assert out["count"] >= 1
    item = out["items"][0]
    assert item["title"]
    assert item["year"] > 1900
    assert item["source"] == "douban"


def test_search_movie_imdb_source(client):
    out = Q.search_movie("Inception", source="imdb", limit=2, client=client)
    assert out["count"] >= 1
    assert out["items"][0]["source"] == "imdb"


def test_get_movie_detail_full_chain(client):
    out = Q.get_movie_detail("douban", "1292052", client=client)
    assert out["found"] is True
    assert out["movie"]["title"]
    assert out["movie"]["genres"] is not None
    assert isinstance(out.get("credits"), list)


def test_get_movie_relations(client):
    out = Q.get_movie_relations("douban", "1292052", limit=5, client=client)
    assert out["count"] >= 1
    assert out["items"][0]["shared_people"] >= 1


def test_search_person_and_movies(client):
    p = Q.search_person("诺兰", source="douban", limit=3, client=client)
    assert p["count"] >= 1
    movies = Q.get_person_movies(p["items"][0]["name_id"], source="douban",
                                 limit=3, client=client)
    assert movies["count"] >= 1


def test_cross_source_view_queryable(client):
    out = Q.compare_cross_source(limit=3, client=client)
    assert "count" in out
    for item in out["items"]:
        assert item["imdb_id"]
        assert item["douban_id"]


def test_table_stats_metadata_channel(client):
    out = Q.get_table_stats("douban", client=client)
    assert out["count"] > 0
    names = {t["table"] for t in out["tables"]}
    assert "douban_main" in names


def test_session_is_read_only_on_server(client):
    """服务端会话确实处于只读事务（MySQL 端确认）。"""
    pool = client._pool(client.db_for_source("douban"))
    conn = pool.acquire()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT @@session.transaction_read_only AS ro")
            row = cur.fetchone()
            assert int(row["ro"]) == 1, "服务端会话必须为只读事务"
    finally:
        pool.release(conn)


def test_schema_map_tables_exist_on_remote(client):
    """schema_map 登记的表在远端真实存在（防表结构漂移）。"""
    res = client.query("imdb2",
                       "SELECT table_name AS t FROM information_schema.tables "
                       "WHERE table_schema = %s LIMIT 200",
                       ("imdb2",), max_limit=200, tool="live_schema_check",
                       allow_metadata=True)
    remote = {r["t"] for r in res.rows}
    for t in ("douban_main", "douban_name_relate", "douban_text"):
        assert t in remote, f"{t} 在远端不存在（schema_map 需更新）"


def test_guard_pure_rejection_has_no_side_effect(client):
    """连续多次写尝试后，远端表行数不变（证明无副作用）。"""
    res = client.query("imdb2", "SELECT COUNT(*) AS n FROM douban_main",
                       max_limit=1, tool="live_count")
    before = int(res.rows[0]["n"])
    for _ in range(3):
        with pytest.raises(McpReadOnlyViolation):
            client.query("imdb2", "DELETE FROM douban_main WHERE id = 1",
                         tool="live_write_probe")
    res2 = client.query("imdb2", "SELECT COUNT(*) AS n FROM douban_main",
                        max_limit=1, tool="live_count")
    assert int(res2.rows[0]["n"]) == before, "护栏拦截后远端行数必须不变"
