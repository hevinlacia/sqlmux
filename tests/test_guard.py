"""guard 语句预检单测(红线所在)。"""

import pytest

from sqlmux.guard import GuardError, classify, guard


class TestClassify:
    def test_read_variants(self):
        for sql in [
            "SELECT 1",
            "select id from t where x = 1",
            "SHOW TABLES",
            "show databases",
            "DESC t",
            "DESCRIBE t",
            "EXPLAIN SELECT 1",
            "  SELECT\n1",
            "WITH cte AS (SELECT 1) SELECT * FROM cte",
            "SELECT * FROM information_schema.tables",
            "SELECT 1;",  # 尾分号
        ]:
            assert classify(sql) == "read", sql

    def test_session_statements(self):
        assert classify("USE wms") == "use"
        assert classify("use `other-db`") == "use"
        assert classify("SET NAMES utf8mb4") == "set"
        assert classify("SET autocommit=1") == "set"
        assert classify("set @@session.sql_mode='x'") == "set"

    def test_writes(self):
        for sql in [
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET a=1",
            "DELETE FROM t",
            "DROP TABLE t",
            "CREATE TABLE t (id int)",
            "TRUNCATE t",
            "ALTER TABLE t ADD c int",
            "GRANT ALL ON *.* TO u",
            "KILL 123",
        ]:
            assert classify(sql) == "write", sql

    def test_unknown(self):
        assert classify("CALL proc(1)") == "write"
        assert classify("LOCK TABLES t READ") == "write"


class TestGuard:
    def test_select_passes(self):
        assert guard("SELECT 1") == "read"

    def test_use_passes(self):
        assert guard("USE other_db") == "use"

    def test_set_passes(self):
        assert guard("SET NAMES utf8mb4") == "set"

    def test_write_rejected(self):
        with pytest.raises(GuardError) as ei:
            guard("UPDATE t SET a=1 WHERE id=1")
        assert ei.value.code == "write-not-permitted"

    def test_ddl_rejected(self):
        with pytest.raises(GuardError):
            guard("CREATE TABLE x (id int)")

    def test_multi_statement_rejected(self):
        with pytest.raises(GuardError) as ei:
            guard("SELECT 1; SELECT 2")
        assert ei.value.code == "multi-statement"

    def test_semicolon_inside_string_ok(self):
        # 引号内分号不算多语句
        assert guard("SELECT * FROM t WHERE note = 'a;b'") == "read"

    def test_empty_rejected(self):
        with pytest.raises(GuardError):
            guard("   ")

    def test_error_message_has_hint(self):
        with pytest.raises(GuardError) as ei:
            guard("DELETE FROM t")
        assert "Archery" in ei.value.message
