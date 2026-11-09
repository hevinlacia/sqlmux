"""asyncio MySQL 协议 server:每 env 一个 loopback 端口,连接级会话状态。

每个连接:
1. handshake(mysql_native_password,SQLMUX_PASSWORD 未设置时接受任意用户)
2. 命令循环:COM_QUERY(guard 预检 → Archery 转发)/ COM_INIT_DB / COM_PING / COM_QUIT
3. USE/SET 本地处理;USE 改写连接级 db_name(影响后续查询的 Archery db 参数)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal

from . import wire
from .archery import ArcheryClient, ArcheryError
from .config import EnvTarget
from .guard import GuardError, extract_db_name, guard

log = logging.getLogger("sqlmux.server")

HOST = "127.0.0.1"
SERVER_VERSION = "8.0.32-sqlmux"

# Archery limit_num:SHOW/DESC/EXPLAIN/内省给大值缓解截断,SELECT 保守
LIMIT_BROWSE = 10000
LIMIT_SELECT = 5000

_BROWSE_RE = re.compile(r"^\s*(SHOW|DESC|DESCRIBE|EXPLAIN|SELECT\s+.*\bFROM\s+information_schema\b)",
                        re.IGNORECASE)

_ERR_CODE = {
    "write-not-permitted": 1142,       # ER_TABLEACCESS_DENIED_ERROR
    "multi-statement": 1064,           # ER_PARSE_ERROR
    "statement-not-supported": 1235,   # ER_NOT_SUPPORTED_YET
    "empty-statement": 1065,           # ER_EMPTY_QUERY
}


class GatewayConnection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 target: EnvTarget, client: ArcheryClient, gateway_password: str) -> None:
        self.reader = reader
        self.writer = writer
        self.target = target
        self.client = client
        self.gateway_password = gateway_password
        self.db_name = target.db_name
        self._salt = wire.new_salt()
        self._conn_id = id(self) & 0xFFFFFFFF

    async def _send_payload(self, seq: int, payload: bytes) -> None:
        self.writer.write(wire.encode_frame(seq, payload))
        await self.writer.drain()

    async def _send_frames(self, frames: list[bytes]) -> None:
        self.writer.write(b"".join(frames))
        await self.writer.drain()

    async def handshake(self) -> bool:
        await self._send_payload(0, wire.build_handshake_v10(self._salt, self._conn_id, SERVER_VERSION))
        try:
            _, payload = await asyncio.wait_for(wire.read_frame(self.reader), timeout=30)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            return False
        try:
            resp = wire.parse_handshake_response(payload)
        except wire.ProtocolError as exc:
            log.warning("%s handshake parse failed: %s", self.target.env, exc)
            return False
        if self.gateway_password and not wire.verify_native_password(
            self.gateway_password, self._salt, resp["auth_response"]
        ):
            await self._send_payload(2, wire.build_err(1045, "Access denied: bad gateway password"))
            return False
        log.info("conn %s user=%s db=%s auth=%s",
                 self.target.env, resp["username"] or "?",
                 resp["database"] or self.db_name,
                 "password" if self.gateway_password else "anonymous")
        await self._send_payload(2, wire.build_ok())
        if resp["database"]:
            self.db_name = resp["database"] or self.db_name
        return True

    async def run(self) -> None:
        if not await self.handshake():
            self.writer.close()
            return
        try:
            while True:
                _, payload = await wire.read_frame(self.reader)
                if not payload:
                    break
                cmd = payload[0]
                if cmd == 0x01:  # COM_QUIT
                    break
                if cmd == 0x0E:  # COM_PING
                    await self._send_payload(1, wire.build_ok())
                elif cmd == 0x02:  # COM_INIT_DB
                    self.db_name = payload[1:].decode("utf-8", "replace") or self.db_name
                    await self._send_payload(1, wire.build_ok())
                elif cmd == 0x03:  # COM_QUERY
                    await self.handle_query(payload[1:].decode("utf-8", "replace"))
                else:
                    await self._send_payload(
                        1, wire.build_err(1047, f"Unsupported command 0x{cmd:02x}; only COM_QUERY is implemented")
                    )
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # 客户端断开,正常
        finally:
            self.writer.close()

    async def handle_query(self, sql: str) -> None:
        log.info("%s SQL: %.200s", self.target.env, sql.replace("\n", " "))
        try:
            kind = guard(sql)
        except GuardError as exc:
            await self._send_payload(1, wire.build_err(_ERR_CODE.get(exc.code, 1105), exc.message))
            return

        if kind == "use":
            db = extract_db_name(sql)
            if db:
                self.db_name = db
                log.info("%s USE -> %s", self.target.env, self.db_name)
            await self._send_payload(1, wire.build_ok())
            return
        if kind == "set":
            await self._send_payload(1, wire.build_ok())
            return

        limit = LIMIT_BROWSE if _BROWSE_RE.match(sql) else LIMIT_SELECT
        try:
            columns, rows = await self.client.query(
                self.target.instance_name, self.db_name, sql, limit
            )
        except ArcheryError as exc:
            await self._send_payload(1, wire.build_err(1105, f"Archery: {exc}"))
            return
        except Exception as exc:  # noqa: BLE001 - 兜底不中断服务
            log.exception("unexpected query failure")
            await self._send_payload(1, wire.build_err(1105, f"Gateway internal error: {type(exc).__name__}"))
            return
        await self._send_frames(wire.build_resultset(columns, rows))


async def serve_target(target: EnvTarget, client: ArcheryClient, gateway_password: str) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = GatewayConnection(reader, writer, target, client, gateway_password)
        await conn.run()

    server = await asyncio.start_server(handle, HOST, target.port)
    log.info("%s listening on %s:%d (instance=%s)", target.env, HOST, target.port, target.instance_name)
    return server


def serve_targets(targets: list[EnvTarget]) -> None:
    """阻塞运行直到 SIGINT/SIGTERM;单次事件循环托管全部监听端口。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    gateway_password = os.environ.get("SQLMUX_PASSWORD", "")
    client = ArcheryClient()

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        servers = [await serve_target(t, client, gateway_password) for t in targets]
        log.info("sqlmux up: %d env(s); Ctrl+C to stop", len(servers))
        await stop.wait()
        for s in servers:
            s.close()
        await client.aclose()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
