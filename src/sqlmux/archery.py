"""Archery HTTP client:认证链 + session 缓存 + /query/ 同步查询。

认证链(与 mysql-archery-query skill 一致,优先级从高到低):
1. Chrome 登录态(~/.agents/scripts/chrome_cookie_auth,可选依赖)
2. env cookies(AGENT_ARCHERY_CSRFTOKEN/SESSIONID)
3. hub 密钥库账密(JT_PERSONAL_ACCOUNT/PASSWORD,经 panel_secret)

降级语义(2026-11-11 修复"死 session 循环"):
- 某来源的 session 被服务端判过期(401/403,或 /query/ 被 302 到登录页返回 HTML),
  该来源在本进程内标记 dead,自动落到下一来源重登重试一次;
- 提取失败(Chrome 未登录/无 cryptography/env 未配置)只是跳过,不标记 dead,
  链自然落到账密登录——账密是最终兜底,不依赖 Chrome 登录态;
- 账密登录前先清空 cookie jar,防止上一来源注入的旧 sessionid 伪装成登录成功;
- 账密登录动作本身失败(密码错/接口异常)也标记 dead,避免反复撞认证接口。

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

# 认证链来源优先级;降级时按序跳过 dead/不可用来源。
_SOURCE_ORDER = ("chrome", "env-cookie", "password")


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
    """Chrome 登录态读取;不可用时静默降级(返回空,链落到下一来源)。"""
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
    source: str = ""


class _Http:
    """极简同步 HTTP(form POST),仅用标准库——服务端零第三方依赖。

    cookie 由 CookieJar 自动维护(登录后 csrftoken/sessionid 自动随请求发送);
    HTTPS 不校验证书(Archery 用内网/自签证书,与既有 CLI 行为一致)。
    """

    def __init__(self, timeout: float = QUERY_TIMEOUT) -> None:
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = self._build_opener(self.jar)
        self.last_status = 0

    @staticmethod
    def _build_opener(jar: http.cookiejar.CookieJar) -> urllib.request.OpenerDirector:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPRedirectHandler(),
        )

    def reset_auth_cookies(self) -> None:
        """清空全部 cookie(换来源登录前调用,防旧 sessionid 误判登录成功)。"""
        self.jar = http.cookiejar.CookieJar()
        self.opener = self._build_opener(self.jar)

    def cookie(self, name: str) -> str:
        for c in self.jar:
            if c.name == name:
                return c.value or ""
        return ""

    def set_cookie(self, name: str, value: str, domain: str) -> None:
        c = http.cookiejar.Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )
        self.jar.set_cookie(c)

    def request(
        self, url: str, data: dict | None = None, headers: dict | None = None
    ) -> tuple[int, str]:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers or {},
            method="POST" if body is not None else "GET",
        )
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
    session 过期或来源失效时按 _SOURCE_ORDER 降级重登,账密为最终兜底。
    """

    def __init__(self) -> None:
        self._http: _Http | None = None
        self._session: _Session | None = None
        self._lock = asyncio.Lock()
        # 被服务端判过期/登录动作失败过的来源,本进程内不再重试。
        self._dead_sources: set[str] = set()

    def _ensure_http(self) -> _Http:
        if self._http is None:
            self._http = _Http()
        return self._http

    async def _login(self) -> _Session:
        """按认证链取一个可用 session;全部来源不可用抛 ArcheryError。"""
        async with self._lock:
            if self._session is not None:
                return self._session
            http = self._ensure_http()
            errors: list[str] = []
            for source in _SOURCE_ORDER:
                if source in self._dead_sources:
                    continue
                try:
                    session = await self._session_from(source, http)
                except ArcheryError as exc:
                    # 登录动作本身失败(如密码错):标记 dead,防止反复撞认证接口。
                    errors.append(f"{source}: {exc}")
                    self._dead_sources.add(source)
                    continue
                if session is None:
                    # 提取不到(Chrome 未登录/env 未配置):静默落到下一来源。
                    errors.append(f"{source}: not available")
                    continue
                self._session = session
                log.info("archery auth via %s", source)
                return session
            raise ArcheryError(
                "No Archery auth source available ("
                + "; ".join(errors)
                + "). Log into Archery in Chrome once, or check hub secrets."
            )

    async def _session_from(self, source: str, http: _Http) -> _Session | None:
        """从指定来源取得 session;取不到返回 None,登录失败抛 ArcheryError。"""
        if source == "chrome":
            csrf, sid = await asyncio.to_thread(_chrome_session)
        elif source == "env-cookie":
            csrf = os.environ.get("AGENT_ARCHERY_CSRFTOKEN", "")
            sid = os.environ.get("AGENT_ARCHERY_SESSIONID", "")
        else:
            csrf, sid = await asyncio.to_thread(self._password_login, http)
        if not (csrf and sid):
            return None
        domain = urlsplit(ARCHERY_URL).hostname or ""
        # cookie 来源(chrome/env)不在 jar 里,手动补一次,后续请求自动带。
        for cname, cval in (("csrftoken", csrf), ("sessionid", sid)):
            if cval and not http.cookie(cname):
                http.set_cookie(cname, cval, domain)
        return _Session(csrf=csrf, sessionid=sid, source=source)

    def _password_login(self, http: _Http) -> tuple[str, str]:
        """账密交互登录(线程内同步执行),返回 (csrf, sessionid);失败抛 ArcheryError。"""
        username = _credential("JT_PERSONAL_ACCOUNT")
        password = _credential("JT_PERSONAL_PASSWORD")
        if not (username and password):
            raise ArcheryError(
                "hub secrets JT_PERSONAL_ACCOUNT/JT_PERSONAL_PASSWORD missing"
            )
        # 先清掉上一来源注入的 cookie,避免旧 sessionid 伪装成登录成功。
        http.reset_auth_cookies()
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

    def _expire_session(self) -> None:
        """当前 session 被服务端判过期:标记来源 dead 并失效缓存。"""
        if self._session is not None:
            log.warning(
                "archery session from %s rejected as expired; falling back",
                self._session.source,
            )
            self._dead_sources.add(self._session.source)
        self._session = None

    @staticmethod
    def _expired_response(status: int, text: str) -> bool:
        """服务端判 session 过期的两种形态:401/403,或 302→登录页(HTML)。"""
        if status in (401, 403):
            return True
        stripped = text.lstrip().lower()
        return stripped.startswith("<!doctype html") or stripped.startswith("<html")

    async def query(
        self, instance_name: str, db_name: str, sql: str, limit: int
    ) -> tuple[list[str], list[list]]:
        """执行只读查询,返回 (columns, rows);失败抛 ArcheryError。

        session 过期时自动降级到下一认证来源重登并重试一次;
        两次都过期说明账密兜底也失效,抛 exhausted 错误而不是让调用方无限重试。
        """
        status, text = 0, ""
        for _attempt in (1, 2):
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
            if not self._expired_response(status, text):
                break
            self._expire_session()
        else:
            raise ArcheryError(
                "Archery session expired and all auth sources are exhausted; "
                "log into Archery in Chrome or check hub secrets."
            )
        if status >= 400:
            raise ArcheryError(f"Archery HTTP {status}: {text[:200]}")
        try:
            payload = json.loads(text)
        except ValueError:
            raise ArcheryError(f"Archery returned non-JSON response: {text[:200]}")
        return parse_query_payload(payload)

    async def aclose(self) -> None:
        self._http = None
        self._session = None


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
