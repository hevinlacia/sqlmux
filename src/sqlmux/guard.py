"""语句预检(红线所在,与协议实现解耦以便单测)。

白名单:SELECT / SHOW / DESC / DESCRIBE / EXPLAIN / WITH
本地拦截:SET ... / USE db(维护会话状态,回 OK 不转发)
拒绝:写/DDL/多语句 → GuardError(code, hint) 映射为 MySQL ERR 包

注释与引号处理:Connector/J 建连会发 "/* version */SELECT @@session...",
注释内/字符串内的分号不能当语句分隔——用引号+注释感知的状态机切分。
"""

from __future__ import annotations

import re

READ_ONLY_RE = re.compile(
    r"^\s*(SELECT|SHOW|DESC|DESCRIBE|EXPLAIN|WITH)\b", re.IGNORECASE
)
WRITE_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE|MERGE|CALL|LOAD|GRANT|REVOKE|"
    r"CREATE|ALTER|DROP|TRUNCATE|RENAME|LOCK|UNLOCK|OPTIMIZE|ANALYZE|"
    r"FLUSH|RESET|SET\s+GLOBAL|SET\s+SESSION|KILL|SHUTDOWN)\b",
    re.IGNORECASE,
)
USE_RE = re.compile(
    r"^\s*USE\s+(?:`([^`]+)`|\"([^\"]+)\"|([\w$]+))\s*;?\s*$", re.IGNORECASE
)
SET_RE = re.compile(r"^\s*SET\b", re.IGNORECASE)


class GuardError(Exception):
    """预检拒绝;消息将作为 MySQL ERR 包文本返回给客户端。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def split_statements(sql: str) -> list[str]:
    """引号/注释感知的语句切分。

    字符串字面量('' "")与标识符引号(``)内的分号不拆;--/#/块注释
    整体剥成空格(Connector/J 前导版本注释、注释内分号都不再误判)。
    """
    parts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote in ("'", '"') and i + 1 < n:
                buf.append(sql[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if sql.startswith("--", i) and (i + 2 >= n or sql[i + 2] in " \t\n"):
            while i < n and sql[i] != "\n":
                i += 1
            buf.append(" ")
            continue
        if ch == "#":
            while i < n and sql[i] != "\n":
                i += 1
            buf.append(" ")
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            buf.append(" ")
            continue
        if ch == ";":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def first_statement(sql: str) -> str:
    """切分后取第一条语句(调用方已保证单语句,取首条剥注释)。"""
    stmts = split_statements(sql)
    return stmts[0] if stmts else ""


def extract_db_name(sql: str) -> str | None:
    """从 USE 语句提取库名(支持反引号/双引号内任意字符);非 USE 返回 None。"""
    m = USE_RE.match(first_statement(sql))
    if not m:
        return None
    return next((g for g in m.groups() if g), None)


def classify(sql: str) -> str:
    """返回 'use' | 'set' | 'read' | 'write' | 'unknown'(应传已剥注释的单语句)。"""
    stripped = sql.strip().rstrip(";").strip()
    if USE_RE.match(stripped):
        return "use"
    if SET_RE.match(stripped):
        return "set"
    if READ_ONLY_RE.match(stripped):
        return "read"
    if WRITE_RE.match(stripped):
        return "write"
    return "unknown"


def check_single_statement(sql: str) -> None:
    stmts = split_statements(sql)
    if not stmts:
        raise GuardError("empty-statement", "Empty statement.")
    if len(stmts) > 1:
        raise GuardError(
            "multi-statement",
            "Multi-statement SQL is not supported through Archery. "
            "Split into separate statements and run them one by one.",
        )


def guard(sql: str) -> str:
    """返回类别('read'|'use'|'set');拒绝时抛 GuardError。"""
    check_single_statement(sql)
    kind = classify(first_statement(sql))
    if kind == "read":
        return "read"
    if kind == "use":
        return "use"
    if kind == "set":
        return "set"
    if kind == "write":
        raise GuardError(
            "write-not-permitted",
            "This gateway is read-only (Archery-backed). "
            "Writes/DDL must go through the Archery SQL workflow ticket; "
            "direct writes are only supported for the test env via mysql-direct-query-write.",
        )
    raise GuardError(
        "statement-not-supported",
        "Only SELECT/SHOW/DESC/DESCRIBE/EXPLAIN (and session SET/USE) "
        "are supported through this gateway.",
    )
