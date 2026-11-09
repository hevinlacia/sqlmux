"""Archery HTTP client:认证链 + session 缓存 + /query/ 同步查询。

认证链(与 mysql-archery-query skill 一致,优先级从高到低):
1. Chrome 登录态(~/.agents/scripts/chrome_cookie_auth,可选依赖)
2. env cookies(AGENT_ARCHERY_CSRFTOKEN/SESSIONID)
3. hub 密钥库账密(AGENT_ARCHERY_USERNAME/PASSWORD,经 panel_secret)

错误契约(双形态,2026-11-03 修复的静默零行坑):
- {"status": 1, "msg": ..., "data": {}}  → 顶层 status 非 0 视为错误
- {"status": 0, "data": {"error": ...}}  → data.error 视为错误
两者必须都检查,单独检查 data.error 会把 SQL 错误当空结果。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("sqlmux.archery")

ARCHERY_URL = os.environ.get(
    "AGENT_ARCHERY_URL", "https://archery.yunlu-tech.cn"
).rstrip("/")

QUERY_TIMEOUT = 60.0


class ArcheryError(Exception):
    """Archery 查询失败(message 将透传为 MySQL ERR 文本)。"""


def _credential(name: str) -> str:
    """env 显式覆盖 > hub 密钥库;不打印值。"""
    value = os.environ.get(name, "")
    if value:
        return value
    sys.path.insert(0, os.path.expanduser("~/.agents/scripts"))
    try:
        from panel_secret import credential  # type: ignore

        return credential(name)
    except Exception:
        return ""


def _chrome_session() -> tuple[str, str]:
    """Chrome 登录态读取;不可用时静默降级(返回空)。"""
    sys.path.insert(0, os.path.expanduser("~/.agents/scripts"))
    try:
        import chrome_cookie_auth as cca  # type: ignore

        domain = urlsplit(ARCHERY_URL).hostname or ""
        csrf_hit = cca.find_latest(domain, "csrftoken")
        session_hit = cca.find_latest(domain, "sessionid")
        if csrf_hit and session_hit:
            return csrf_hit["value"], session_hit["value"]
        return "", ""
    except Exception as exc:  # pragma: no cover - 依赖本机 Chrome 环境
        log.debug("chrome auth unavailable: %s", exc)
        return "", ""


@dataclass
class _Session:
    csrf: str
    sessionid: str


class ArcheryClient:
    """进程级共享 client:一个 httpx.AsyncClient + 一把 login 锁。"""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._session: _Session | None = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(verify=False, timeout=QUERY_TIMEOUT)
        return self._client

    async def _login(self) -> _Session:
        async with self._lock:
            if self._session is not None:
                return self._session
            client = await self._ensure_client()

            csrf, sessionid = await asyncio.to_thread(_chrome_session)
            source = "chrome"
            if not (csrf and sessionid):
                csrf = os.environ.get("AGENT_ARCHERY_CSRFTOKEN", "")
                sessionid = os.environ.get("AGENT_ARCHERY_SESSIONID", "")
                source = "env-cookie"
            if not (csrf and sessionid):
                username = _credential("JT_PERSONAL_ACCOUNT")
                password = _credential("JT_PERSONAL_PASSWORD")
                if not (username and password):
                    raise ArcheryError(
                        "No Archery auth source available. "
                        "Log into Archery in Chrome once, or configure hub secrets."
                    )
                resp = await client.get(f"{ARCHERY_URL}/login/")
                resp.raise_for_status()
                csrf = resp.cookies.get("csrftoken", "")
                login_resp = await client.post(
                    f"{ARCHERY_URL}/authenticate/",
                    data={"username": username, "password": password},
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-CSRFToken": csrf,
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": f"{ARCHERY_URL}/login/",
                        "Origin": ARCHERY_URL,
                    },
                )
                login_resp.raise_for_status()
                sessionid = login_resp.cookies.get("sessionid", "")
                csrf = login_resp.cookies.get("csrftoken") or csrf
                if not sessionid:
                    server_msg = ""
                    try:
                        server_msg = str(login_resp.json().get("msg", "")).strip()
                    except Exception:
                        server_msg = login_resp.text[:200]
                    raise ArcheryError(f"Archery password login failed: {server_msg or 'no sessionid'}")
                source = "password"
            log.info("archery auth via %s", source)
            self._session = _Session(csrf=csrf, sessionid=sessionid)
            return self._session

    def _invalidate(self) -> None:
        self._session = None

    async def query(self, instance_name: str, db_name: str, sql: str, limit: int) -> tuple[list[str], list[list]]:
        """执行只读查询,返回 (columns, rows);失败抛 ArcheryError。"""
        session = await self._login()
        client = await self._ensure_client()
        resp = await client.post(
            f"{ARCHERY_URL}/query/",
            data={
                "instance_name": instance_name,
                "db_name": db_name,
                "schema_name": "",
                "tb_name": "",
                "sql_content": sql,
                "limit_num": str(limit),
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-CSRFToken": session.csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Cookie": f"csrftoken={session.csrf}; sessionid={session.sessionid}",
                "Referer": f"{ARCHERY_URL}/sqlquery/",
                "Origin": ARCHERY_URL,
                "User-Agent": "Mozilla/5.0",
            },
        )
        if resp.status_code in (401, 403):
            self._invalidate()
            raise ArcheryError("Archery session expired; retry the query.")
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError:
            raise ArcheryError(f"Archery returned non-JSON response: {resp.text[:200]}")
        return parse_query_payload(payload)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def parse_query_payload(payload: dict) -> tuple[list[str], list[list]]:
    """双形态错误检查 + 提取 (column_list, rows)。"""
    data = payload.get("data") or {}
    error = data.get("error")
    if not error and payload.get("status") not in (0, None):
        error = str(payload.get("msg") or "unknown Archery error")
    if error:
        raise ArcheryError(str(error))
    columns = list(data.get("column_list") or [])
    rows = [list(r) for r in (data.get("rows") or [])]
    return columns, rows
