"""MySQL 文本协议子集编解码。

只实现网关需要的部分:
- 通用帧(header + payload,sequence 递增)
- HandshakeV10 / HandshakeResponse41(宽松解析:只取 user/database/auth response)
- mysql_native_password scramble 校验(SQLMUX_PASSWORD 可选)
- OK / ERR / EOF 包
- ResultSet(ColumnDefinition41 + TextRow,全列 VAR_STRING 文本编码)

协议参考: dev.mysql.com/doc/internals/en/client-server-protocol.html
不实现:SSL、压缩、prepared statements、resultset 分片(单包 >16MB 场景不存在,
Archery 侧行数有限)。
"""

from __future__ import annotations

import hashlib
import os
import struct

# ---- capability flags(客户端服务器都按此协商) ----
CLIENT_LONG_PASSWORD = 1
CLIENT_PROTOCOL_41 = 1 << 9
CLIENT_SECURE_CONNECTION = 1 << 15
CLIENT_PLUGIN_AUTH = 1 << 19
CAPABILITIES = (
    CLIENT_LONG_PASSWORD | CLIENT_PROTOCOL_41 | CLIENT_SECURE_CONNECTION | CLIENT_PLUGIN_AUTH
)
SERVER_STATUS_AUTOCOMMIT = 0x0002

# ---- column meta(全部按 utf8mb4 文本字符串呈现) ----
COLLATION_UTF8MB4_GENERAL_CI = 45
MYSQL_TYPE_VAR_STRING = 253

MAX_PACKET_PAYLOAD = 0xFFFFFF - 1  # 保守:单帧 16MB 以内

SQLSTATE_GENERIC = "HY000"


class ProtocolError(Exception):
    """无法继续的协议错误(连接应关闭)。"""


# ---------- 帧层 ----------

def encode_frame(seq: int, payload: bytes) -> bytes:
    if len(payload) > MAX_PACKET_PAYLOAD:
        raise ProtocolError(f"payload too large: {len(payload)}")
    return struct.pack("<I", len(payload))[:3] + bytes([seq & 0xFF]) + payload


async def read_frame(reader) -> tuple[int, bytes]:
    """读一帧,返回 (seq, payload)。"""
    header = await reader.readexactly(4)
    length = struct.unpack("<I", header[:3] + b"\x00")[0]
    seq = header[3]
    payload = await reader.readexactly(length)
    return seq, payload


def parse_payload_length(header: bytes) -> int:
    return struct.unpack("<I", header[:3] + b"\x00")[0]


# ---------- length-encoded integer ----------

def encode_lenenc_int(value: int) -> bytes:
    if value < 251:
        return bytes([value])
    if value < 1 << 16:
        return b"\xfc" + struct.pack("<H", value)
    if value < 1 << 24:
        return b"\xfd" + value.to_bytes(3, "little")
    return b"\xfe" + struct.pack("<Q", value)


def encode_lenenc_str(data: bytes) -> bytes:
    return encode_lenenc_int(len(data)) + data


# ---------- handshake ----------

def build_handshake_v10(salt: bytes, conn_id: int, server_version: str) -> bytes:
    """server version 形如 '8.0.32-sqlmux';salt 长度 20(8+12)。"""
    part1, part2 = salt[:8], salt[8:20]
    buf = bytearray()
    buf += b"\x0a"                                     # protocol version 10
    buf += server_version.encode() + b"\x00"
    buf += struct.pack("<I", conn_id)
    buf += part1 + b"\x00"                             # 8 bytes + NUL
    buf += struct.pack("<H", CAPABILITIES & 0xFFFF)    # capability lower
    buf += bytes([COLLATION_UTF8MB4_GENERAL_CI])
    buf += struct.pack("<H", SERVER_STATUS_AUTOCOMMIT)
    buf += struct.pack("<H", (CAPABILITIES >> 16) & 0xFFFF)  # capability upper
    buf += bytes([21])                                 # auth data len (20 + NUL)
    buf += b"\x00" * 10                                # reserved
    buf += part2                                       # 12 bytes(不带 NUL,省 1 字节,客户端兼容)
    buf += b"mysql_native_password\x00"
    return bytes(buf)


def parse_handshake_response(payload: bytes) -> dict:
    """宽松解析客户端握手响应;失败抛 ProtocolError。

    只取 username/database/auth_response;不校验其余字段。
    """
    if len(payload) < 32:
        raise ProtocolError("handshake response too short")
    caps, max_packet, charset = struct.unpack("<IIb", payload[:9])
    off = 32  # 4+4+1+23 reserved
    username, off = _read_nul_str(payload, off)
    auth_response = b""
    if caps & (1 << 22):  # CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA
        val, off = _read_lenenc_int(payload, off)
        auth_response, off = payload[off : off + val], off + val
    elif caps & (1 << 15):  # CLIENT_SECURE_CONNECTION: 1 byte len + data
        alen = payload[off]
        auth_response, off = payload[off + 1 : off + 1 + alen], off + 1 + alen
    else:  # 老协议 NUL 结尾
        auth_response, off = _read_nul_str(payload, off)
    database = ""
    if caps & (1 << 3):  # CLIENT_CONNECT_WITH_DB
        db_bytes, off = _read_nul_str(payload, off)
        database = db_bytes.decode("utf-8", "replace")
    return {
        "capabilities": caps,
        "username": username.decode("utf-8", "replace"),
        "database": database,
        "auth_response": auth_response,
    }


def _read_nul_str(buf: bytes, off: int) -> tuple[bytes, int]:
    end = buf.find(b"\x00", off)
    if end < 0:
        raise ProtocolError("unterminated string in handshake response")
    return buf[off:end], end + 1


def _read_lenenc_int(buf: bytes, off: int) -> tuple[int, int]:
    first = buf[off]
    if first < 251:
        return first, off + 1
    if first == 0xFC:
        return struct.unpack("<H", buf[off + 1 : off + 3])[0], off + 3
    if first == 0xFD:
        return int.from_bytes(buf[off + 1 : off + 4], "little"), off + 4
    if first == 0xFE:
        return struct.unpack("<Q", buf[off + 1 : off + 9])[0], off + 9
    raise ProtocolError("bad lenenc int")


# ---------- auth(mysql_native_password) ----------

def new_salt() -> bytes:
    return os.urandom(20)


def verify_native_password(password: str, salt: bytes, auth_response: bytes) -> bool:
    """校验 client reply = SHA1(pw) XOR SHA1(salt + SHA1(SHA1(pw)))。

    空密码:合法 reply 为空 bytes。
    """
    pw_bytes = password.encode("utf-8")
    if not pw_bytes:
        return len(auth_response) == 0
    if not auth_response:
        return False
    stage1 = hashlib.sha1(pw_bytes).digest()
    stage2 = hashlib.sha1(stage1).digest()
    expected = hashlib.sha1(salt + stage2).digest()
    candidate = bytes(a ^ b for a, b in zip(auth_response, expected))
    return hashlib.sha1(candidate).digest() == stage2


# ---------- 响应包 ----------

def build_ok(affected_rows: int = 0, last_insert_id: int = 0) -> bytes:
    buf = bytearray(b"\x00")
    buf += encode_lenenc_int(affected_rows)
    buf += encode_lenenc_int(last_insert_id)
    buf += struct.pack("<HH", SERVER_STATUS_AUTOCOMMIT, 0)  # status flags + warnings
    return bytes(buf)


def build_err(code: int, message: str, sqlstate: str = SQLSTATE_GENERIC) -> bytes:
    buf = bytearray(b"\xff")
    buf += struct.pack("<H", code)
    buf += b"#"
    buf += sqlstate[:5].encode().ljust(5, b"0")
    buf += message.encode("utf-8")
    return bytes(buf)


def build_eof(warnings: int = 0, status: int = SERVER_STATUS_AUTOCOMMIT) -> bytes:
    return b"\xfe" + struct.pack("<HH", warnings, status)


def build_column_definition(name: str) -> bytes:
    """ColumnDefinition41,全部按 VAR_STRING 文本列。"""
    buf = bytearray()
    buf += encode_lenenc_str(b"def")
    buf += encode_lenenc_str(b"")
    buf += encode_lenenc_str(b"")
    buf += encode_lenenc_str(b"")
    buf += encode_lenenc_str(name.encode("utf-8"))
    buf += encode_lenenc_str(name.encode("utf-8"))
    buf += encode_lenenc_int(0x0C)                     # fixed fields length
    buf += struct.pack("<H", COLLATION_UTF8MB4_GENERAL_CI)
    buf += struct.pack("<I", 1024)                     # column length
    buf += bytes([MYSQL_TYPE_VAR_STRING])
    buf += struct.pack("<H", 0)                        # flags
    buf += bytes([0])                                  # decimals
    buf += struct.pack("<H", 0)                        # filler
    return bytes(buf)


def build_resultset(columns: list[str], rows: list[list], start_seq: int = 1) -> list[bytes]:
    """编码完整 resultset(多帧,payload 依次为:列数/列定义/EOF/行/EOF)。

    start_seq 默认 1:客户端命令包 seq=0,响应序列必须从 1 开始。
    """
    frames: list[bytes] = []
    seq = start_seq

    def push(payload: bytes) -> None:
        nonlocal seq
        frames.append(encode_frame(seq, payload))
        seq += 1

    push(encode_lenenc_int(len(columns)))
    for col in columns:
        push(build_column_definition(col))
    push(build_eof())
    for row in rows:
        cells = bytearray()
        for cell in row:
            if cell is None:
                cells += b"\xfb"
            else:
                text = cell if isinstance(cell, str) else str(cell)
                cells += encode_lenenc_str(text.encode("utf-8"))
        push(bytes(cells))
    push(build_eof())
    return frames


def build_query_error(code: int, message: str) -> bytes:
    """COM_QUERY 的响应帧(seq 从命令起始重置,由调用方管 seq;此处 seq=1)。"""
    return encode_frame(1, build_err(code, message))
