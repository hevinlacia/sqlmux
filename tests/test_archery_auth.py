"""Archery 认证链单测:过期降级/提取失败落到账密/登录动作与 cookie 清理。

不依赖真实 Archery 与 Chrome:FakeHttp 模拟响应脚本,session 来源注入 canned session。
"""

import asyncio
import json

import pytest

from sqlmux.archery import ArcheryClient, ArcheryError, _Session  # noqa: E402

LOGIN_PAGE = "<!DOCTYPE html>\n<html>\n<head>\n<title>Archery</title>\n</head></html>"


class FakeHttp:
    """_Http 替身:按脚本依次返回 (status, text[, set_cookies]),记录请求路径。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.paths: list[str] = []
        self.cookies: dict[str, str] = {}

    def cookie(self, name: str) -> str:
        return self.cookies.get(name, "")

    def set_cookie(self, name: str, value: str, domain: str) -> None:
        self.cookies[name] = value

    def reset_auth_cookies(self) -> None:
        self.cookies.clear()

    def request(self, url, data=None, headers=None):
        from urllib.parse import urlsplit

        self.paths.append(urlsplit(url).path)
        item = self.responses.pop(0)
        status, text = item[0], item[1]
        if len(item) > 2:
            self.cookies.update(item[2])
        return status, text


def _ok_payload() -> str:
    return json.dumps({"status": 0, "data": {"column_list": ["1"], "rows": [[1]]}})


def _client(http: FakeHttp, sessions: dict) -> ArcheryClient:
    """构造 client:注入 fake http 与来源→session 脚本(缺来源=提取不到)。"""

    async def fake_session_from(source, _http):
        return sessions.get(source)

    client = ArcheryClient()
    client._http = http
    client._session_from = fake_session_from  # type: ignore[method-assign]
    return client


def test_expired_response_detection():
    assert ArcheryClient._expired_response(401, "")
    assert ArcheryClient._expired_response(403, "forbidden")
    assert ArcheryClient._expired_response(200, LOGIN_PAGE)
    assert ArcheryClient._expired_response(200, "  <HTML><body>login</body>")
    assert not ArcheryClient._expired_response(200, _ok_payload())
    assert not ArcheryClient._expired_response(500, "boom")


def test_expired_chrome_session_falls_back_to_password():
    # chrome session 被服务端判过期(302→登录页 HTML)→ 标记 dead → 账密重登重试成功
    http = FakeHttp([(200, LOGIN_PAGE), (200, _ok_payload())])
    sessions = {
        "chrome": _Session("c1", "s1", "chrome"),
        "password": _Session("c2", "s2", "password"),
    }
    client = _client(http, sessions)
    columns, rows = asyncio.run(client.query("inst", "wms", "SELECT 1", 100))
    assert rows == [[1]]
    assert client._dead_sources == {"chrome"}
    assert http.paths == ["/query/", "/query/"]  # 失败后自动重试了一次


def test_extraction_miss_goes_password_without_dead_mark():
    # Chrome/env 都提取不到(未登录/未配置)→ 静默落到账密,不标记 dead
    http = FakeHttp([(200, _ok_payload())])
    sessions = {
        "chrome": None,
        "env-cookie": None,
        "password": _Session("c", "s", "password"),
    }
    client = _client(http, sessions)
    _, rows = asyncio.run(client.query("inst", "wms", "SELECT 1", 100))
    assert rows == [[1]]
    assert client._dead_sources == set()


def test_login_action_failure_marks_source_dead():
    # 提取动作本身抛错(如 chrome 环境坏了)→ 标记 dead → 落到账密
    http = FakeHttp([(200, _ok_payload())])
    sessions = {"password": _Session("c", "s", "password")}

    async def fake_session_from(source, _http):
        if source == "chrome":
            raise ArcheryError("chrome env broken")
        return sessions.get(source)

    client = ArcheryClient()
    client._http = http
    client._session_from = fake_session_from  # type: ignore[method-assign]
    _, rows = asyncio.run(client.query("inst", "wms", "SELECT 1", 100))
    assert rows == [[1]]
    assert client._dead_sources == {"chrome"}


def test_all_sources_expired_raises_exhausted():
    http = FakeHttp([(200, LOGIN_PAGE), (403, "forbidden")])
    sessions = {
        "chrome": _Session("c1", "s1", "chrome"),
        "password": _Session("c2", "s2", "password"),
    }
    client = _client(http, sessions)
    with pytest.raises(ArcheryError, match="exhausted"):
        asyncio.run(client.query("inst", "wms", "SELECT 1", 100))
    assert client._dead_sources == {"chrome", "password"}


def test_password_login_success_clears_stale_cookies(monkeypatch):
    monkeypatch.setattr(
        "sqlmux.archery._credential",
        lambda name: "user" if name == "JT_PERSONAL_ACCOUNT" else "pass",
    )
    http = FakeHttp(
        [
            (200, "", {"csrftoken": "fresh-csrf"}),  # GET /login/
            (
                200,
                json.dumps({"status": 0, "msg": "ok"}),
                {"sessionid": "fresh-sid"},
            ),  # POST /authenticate/
        ]
    )
    http.set_cookie(
        "sessionid", "stale-sid", "archery.example.com"
    )  # 上一来源注入的死 cookie
    client = ArcheryClient()
    client._http = http
    csrf, sid = client._password_login(http)
    assert (csrf, sid) == ("fresh-csrf", "fresh-sid")
    assert http.cookies["sessionid"] == "fresh-sid"  # stale 已被 reset,不会被误返回
    assert http.paths == ["/login/", "/authenticate/"]


def test_password_login_rejects_stale_sessionid_trap(monkeypatch):
    # 回归:登录失败但 jar 残留旧 sessionid 时,不得误判登录成功
    monkeypatch.setattr(
        "sqlmux.archery._credential",
        lambda name: "user" if name == "JT_PERSONAL_ACCOUNT" else "pass",
    )
    http = FakeHttp(
        [
            (200, "", {"csrftoken": "c"}),
            (200, json.dumps({"status": 1, "msg": "bad password"})),  # 不 set sessionid
        ]
    )
    http.set_cookie("sessionid", "stale-sid", "archery.example.com")
    client = ArcheryClient()
    client._http = http
    with pytest.raises(ArcheryError, match="password login failed"):
        client._password_login(http)
