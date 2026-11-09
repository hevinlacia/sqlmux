"""端到端集成测试:真实 pymysql 客户端 × mock Archery 后端。

验证完整协议栈(handshake/COM_QUERY/resultset/ERR)与 guard 红线,
不依赖真实 Archery。
"""

import asyncio

import pytest

pymysql = pytest.importorskip("pymysql")

from sqlmux import server as srv  # noqa: E402
from sqlmux.archery import ArcheryClient  # noqa: E402
from sqlmux.config import EnvTarget  # noqa: E402


class FakeArchery(ArcheryClient):
    """记录调用并返回固定结果集,不发起任何网络请求。"""

    def __init__(self) -> None:
        super().__init__()
        self.queries: list[tuple[str, str, str, int]] = []

    async def query(self, instance_name, db_name, sql, limit):
        self.queries.append((instance_name, db_name, sql, limit))
        upper = sql.strip().upper()
        if upper.startswith("SELECT 1"):
            return ["1"], [[1]]
        if upper.startswith("SHOW TABLES"):
            return ["Tables_in_wms"], [["shipment_header"], ["receipt_header"]]
        return ["col"], [["v1"], ["v2"]]


def _run(pw: str = "", fn=None) -> None:
    async def scenario():
        target = EnvTarget(env="uat-sea", port=0, instance_name="cwhsea-uat", db_name="wms", is_pro=False)
        fake = FakeArchery()
        s = await srv.serve_target(target, fake, gateway_password=pw)
        port = s.sockets[0].getsockname()[1]

        # pymysql 是阻塞 socket 客户端,必须在独立线程跑;
        # 否则 recv 会冻结事件循环,服务端回调永不调度(死锁)。
        def client_ops():
            conn = pymysql.connect(host="127.0.0.1", port=port, user="anyone",
                                   database="wms", password=pw)
            try:
                if fn:
                    fn(cur=conn.cursor(), fake=fake)
            finally:
                conn.close()

        try:
            await asyncio.to_thread(client_ops)
        finally:
            s.close()
            await s.wait_closed()
            await fake.aclose()

    asyncio.run(scenario())


def test_select_roundtrip():
    def go(cur, fake):
        cur.execute("SELECT 1")
        row = cur.fetchall()
        assert row[0][0] in (1, "1")
        assert fake.queries[-1][2] == "SELECT 1"

    _run(fn=go)


def test_show_tables_resultset():
    def go(cur, fake):
        cur.execute("SHOW TABLES")
        rows = cur.fetchall()
        assert len(rows) == 2
        # SHOW 类语句走提升后的 limit
        assert fake.queries[-1][3] == srv.LIMIT_BROWSE

    _run(fn=go)


def test_write_rejected_with_1142():
    def go(cur, fake):
        with pytest.raises(pymysql.err.OperationalError) as ei:
            cur.execute("UPDATE t SET a=1 WHERE id=1")
        assert ei.value.args[0] == 1142
        assert "read-only" in ei.value.args[1]
        assert not fake.queries

    _run(fn=go)


def test_multi_statement_rejected():
    def go(cur, fake):
        with pytest.raises(pymysql.err.ProgrammingError) as ei:
            cur.execute("SELECT 1; SELECT 2")
        assert ei.value.args[0] == 1064

    _run(fn=go)


def test_use_switches_db_state():
    def go(cur, fake):
        cur.execute("USE `other-db`")
        cur.execute("SELECT 1")
        assert fake.queries[-1][1] == "other-db"
        cur.execute("USE back_to_wms")
        cur.execute("SELECT 1")
        assert fake.queries[-1][1] == "back_to_wms"

    _run(fn=go)


def test_session_set_is_local():
    def go(cur, fake):
        cur.execute("SET NAMES utf8mb4")
        cur.execute("SET autocommit=1")
        assert not fake.queries  # SET 不转发

    _run(fn=go)


def test_bad_gateway_password_rejected():
    # 客户端密码为空,网关密码校验开启时应被拒(mysql_native_password ERR)
    with pytest.raises(pymysql.err.OperationalError) as ei:
        _run(pw="secret", fn=None)
    assert ei.value.args[0] in (1045, 2013, 1044)
