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
import http.cookiejar
import json
import logging
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

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


class _Http:
    """极简同步 HTTP(form POST),仅用标准库——服务端零第三方依赖。

    cookie 由 CookieJar 自动维护(登录后 csrftoken/sessionid 自动随请求发送);
    HTTPS 不校验证书(Archery 用内网/自签证书,与既有 CLI 行为一致)。
    """

    def __init__(self, timeout: float = QUERY_TIMEOUT) -> None:
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPRedirectHandler(),
        )
        self.last_status = 0

    def cookie(self, name: str) -> str:
        for c in self.jar:
            if c.name == name:
                return c.value or ""
        return ""

    def set_cookie(self, name: str, value: str, domain: str) -> None:
        c = http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=False,
            path="/", path_specified=True, secure=True, expires=None,
            discard=False, comment=None, comment_url=None, rest={},
        )
        self.jar.set_cookie(c)

    def request(self, url: str, data: dict | None = None, headers: dict | None = None) -> tuple[int, str]:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST" if body is not None else "GET")
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                self.last_status = resp.status
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            self.last_status = err.code
            try:
                text = err.read().decode("utf-8", "replace")
            except Exception:
                text = ""
            return err.code, text


class ArcheryClient:
    """进程级共享连接:一个 _Http(带 cookie jar) + 一把 login 锁。

    全部 HTTP 调用经 asyncio.to_thread 落到线程,不阻塞事件循环。
    """

    def __init__(self) -> None:
        self._http: _Http | None = None
        self._session: _Session | None = None
        self._lock = asyncio.Lock()

    def _ensure_http(self) -> _Http:
        if self._http is None:
            self._http = _Http()
        return self._http

    async def _login(self) -> _Session:
        async with self._lock:
            if self._session is not None:
                return self._session
            http = self._ensure_http()

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
                domain = urlsplit(ARCHERY_URL).hostname or ""

                def do_login() -> tuple[str, str]:
                    status, _ = http.request(f"{ARCHERY_URL}/login/")
                    if status >= 400:
                        raise ArcheryError(f"Archery login page HTTP {status}")
                    csrf_cookie = http.cookie("csrftoken")
                    if not csrf_cookie:
                        raise ArcheryError("Archery login page returned no csrftoken")
                    status, text = http.request(
                        f"{ARCHERY_URL}/authenticate/",
                        data={"username": username, "password": password},
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "X-CSRFToken": csrf_cookie,
                            "X-Requested-With": "XMLHttpRequest",
                            "Referer": f"{ARCHERY_URL}/login/",
                            "Origin": ARCHERY_URL,
                        },
                    )
                    sid = http.cookie("sessionid")
                    if status >= 400 or not sid:
                        server_msg = ""
                        try:
                            server_msg = str(json.loads(text).get("msg", "")).strip()
                        except Exception:
                            server_msg = text[:200]
                        raise ArcheryError(
                            f"Archery password login failed (HTTP {status}): {server_msg or 'no sessionid'}"
                        )
                    return http.cookie("csrftoken") or csrf_cookie, sid

                csrf, sessionid = await asyncio.to_thread(do_login)
                source = "password"
            else:
                domain = urlsplit(ARCHERY_URL).hostname or ""

            # cookie 来源(chrome/env)不在 jar 里,手动补一次,后续请求自动带。
            for cname, cval in (("csrftoken", csrf), ("sessionid", sessionid)):
                if cval and not http.cookie(cname):
                    http.set_cookie(cname, cval, domain)
            log.info("archery auth via %s", source)
            self._session = _Session(csrf=csrf, sessionid=sessionid)
            return self._session

    def _invalidate(self) -> None:
        self._session = None

    async def query(self, instance_name: str, db_name: str, sql: str, limit: int) -> tuple[list[str], list[list]]:
        """执行只读查询,返回 (columns, rows);失败抛 ArcheryError。"""
        session = await self._login()
        http = self._ensure_http()

        def do_query() -> tuple[int, str]:
            return http.request(
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
                    "Referer": f"{ARCHERY_URL}/sqlquery/",
                    "Origin": ARCHERY_URL,
                    "User-Agent": "Mozilla/5.0",
                },
            )

        status, text = await asyncio.to_thread(do_query)
        if status in (401, 403):
            self._invalidate()
            raise ArcheryError("Archery session expired; retry the query.")
        if status >= 400:
            raise ArcheryError(f"Archery HTTP {status}: {text[:200]}")
        try:
            payload = json.loads(text)
        except ValueError:
            raise ArcheryError(f"Archery returned non-JSON response: {text[:200]}")
        return parse_query_payload(payload)

    async def aclose(self) -> None:
        self._http = None


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
