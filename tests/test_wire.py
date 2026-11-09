"""wire 协议编解码单测。"""

import hashlib

from sqlmux import wire


class TestLenenc:
    def test_int_boundaries(self):
        assert wire.encode_lenenc_int(0) == b"\x00"
        assert wire.encode_lenenc_int(250) == b"\xfa"
        assert wire.encode_lenenc_int(251) == b"\xfc\xfb\x00"
        assert wire.encode_lenenc_int(65535) == b"\xfc\xff\xff"
        assert wire.encode_lenenc_int(1 << 16) == b"\xfd\x00\x00\x01"

    def test_str_roundtrip_shape(self):
        out = wire.encode_lenenc_str("abc".encode())
        assert out == b"\x03abc"


class TestFrames:
    def test_frame_roundtrip(self):
        frame = wire.encode_frame(3, b"hello")
        assert frame[:3] == b"\x05\x00\x00"
        assert frame[3] == 3
        assert frame[4:] == b"hello"

    def test_err_packet(self):
        pkt = wire.build_err(1142, "denied: reason")
        assert pkt[0] == 0xFF
        assert pkt[1:3] == b"\x76\x04"  # 1142 LE
        assert pkt[3] == ord("#")
        assert b"denied: reason" in pkt

    def test_ok_packet(self):
        pkt = wire.build_ok()
        assert pkt[0] == 0x00

    def test_resultset_structure(self):
        frames = wire.build_resultset(["a", "b"], [["1", "x"], [None, "y"]])
        # 列数帧
        assert frames[0][4:] == b"\x02"
        # 两列定义 + EOF + 两行 + EOF = 6 帧后续
        assert len(frames) == 1 + 2 + 1 + 2 + 1
        # EOF 帧
        assert frames[3][4] == 0xFE
        # 第一行: '1','x'
        row = frames[4][4:]
        assert row == b"\x011\x01x"
        # 第二行: NULL,'y'
        row = frames[5][4:]
        assert row == b"\xfb\x01y"


class TestHandshake:
    def test_build_and_parse_roundtrip(self):
        salt = bytes(range(20))
        hs = wire.build_handshake_v10(salt, 42, "8.0.32-sqlmux")
        assert hs[0] == 10
        assert b"mysql_native_password" in hs
        # 客户端响应(Connector/J 风格): PROTOCOL_41|SECURE_CONNECTION|CONNECT_WITH_DB|PLUGIN_AUTH
        caps = wire.CLIENT_PROTOCOL_41 | (1 << 15) | (1 << 3) | (1 << 19)
        auth = b"\x11" * 20
        payload = (
            caps.to_bytes(4, "little")
            + (1 << 24).to_bytes(4, "little")
            + bytes([45])
            + b"\x00" * 23
            + b"sqlmux\x00"
            + bytes([len(auth)])
            + auth
            + b"wms\x00"
            + b"mysql_native_password\x00"
        )
        parsed = wire.parse_handshake_response(payload)
        assert parsed["username"] == "sqlmux"
        assert parsed["database"] == "wms"
        assert parsed["auth_response"] == auth

    def test_native_password_verify(self):
        salt = bytes(range(20))
        pw = "s3cret"
        stage1 = hashlib.sha1(pw.encode()).digest()
        stage2 = hashlib.sha1(stage1).digest()
        reply = bytes(a ^ b for a, b in zip(stage1, hashlib.sha1(salt + stage2).digest()))
        assert wire.verify_native_password(pw, salt, reply)
        assert not wire.verify_native_password("wrong", salt, reply)
        # 空密码场景
        assert wire.verify_native_password("", salt, b"")
        assert not wire.verify_native_password("", salt, b"x")
        assert not wire.verify_native_password(pw, salt, b"")
