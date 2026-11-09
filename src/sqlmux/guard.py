"""语句预检(红线所在,与协议实现解耦以便单测)。

白名单:SELECT / SHOW / DESC / DESCRIBE / EXPLAIN
本地拦截:SET ... / USE db(维护会话状态,回 OK 不转发)
拒绝:写/DDL/多语句 → GuardError(code, hint) 映射为 MySQL ERR 包
"""

from __future__ import annotations

import re

READ_ONLY_RE = re.compile(
    r"^\s*(SELECT|SHOW|DESC|DESCRIBE|EXPLAIN|WITH|ANSWER)\b", re.IGNORECASE
)
WRITE_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE|MERGE|CALL|LOAD|GRANT|REVOKE|"
    r"CREATE|ALTER|DROP|TRUNCATE|RENAME|LOCK|UNLOCK|OPTIMIZE|ANALYZE|"
    r"FLUSH|RESET|SET\s+GLOBAL|SET\s+SESSION|KILL|SHUTDOWN)\b",
    re.IGNORECASE,
)
USE_RE = re.compile(r"^\s*USE\s+[`\"']?([\w$]+)[`\"']?\s*;?\s*$", re.IGNORECASE)
SET_RE = re.compile(r"^\s*SET\b", re.IGNORECASE)
# 朴素多语句检测:跳过引号内的分号
_SPLit_RE = re.compile(r""";(?=(?:[^'"`]*['"`][^'"`]*['"`])*[^'"`]*$)""")


class GuardError(Exception):
    """预检拒绝;消息将作为 MySQL ERR 包文本返回给客户端。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def classify(sql: str) -> str:
    """返回 'use' | 'set' | 'read' | 'write' | 'unknown'。"""
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
    body = sql.strip().rstrip(";").strip()
    if not body:
        raise GuardError("empty-statement", "Empty statement.")
    # 多语句:非引号内出现分号分隔的第二段非空内容
    parts = [p for p in _SPLit_RE.split(body) if p.strip()]
    if len(parts) > 1:
        raise GuardError(
            "multi-statement",
            "Multi-statement SQL is not supported through Archery. "
            "Split into separate statements and run them one by one.",
        )


def guard(sql: str) -> str:
    """返回类别('read'|'use'|'set');拒绝时抛 GuardError。"""
    check_single_statement(sql)
    kind = classify(sql)
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
