"""guard 语句预检单测(红线所在)。"""

import pytest

from sqlmux.guard import GuardError, classify, extract_db_name, guard


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

    def test_connector_j_leading_comment(self):
        # Connector/J 建连首条: /* version */SELECT @@session...
        sql = "/* mysql-connector-java-8.0.28 (Rev) */SELECT  @@session.auto_increment_increment, @@session.character_set_client"
        assert guard(sql) == "read"

    def test_comment_semicolon_not_multistatement(self):
        assert guard("/* a;b;c */ SELECT 1") == "read"
        assert guard("SELECT 1 -- trailing;comment") == "read"
        assert guard("-- header\nSELECT 1") == "read"

    def test_string_semicolon_not_multistatement(self):
        assert guard("SELECT * FROM t WHERE note = 'a;b' AND x = \"d;e\"") == "read"

    def test_use_with_leading_comment(self):
        assert extract_db_name("/* c */ USE `other-db`") == "other-db"


class TestRewriteForArchery:
    """Archery 不支持 DESC/DESCRIBE,网关翻译为标准 SHOW COLUMNS。"""

    def test_desc_rewritten(self):
        from sqlmux.server import rewrite_for_archery
        assert rewrite_for_archery("DESC shipment_header") == "SHOW COLUMNS FROM shipment_header"
        assert rewrite_for_archery("desc t") == "SHOW COLUMNS FROM t"
        assert rewrite_for_archery("DESCRIBE `wms`.`t`") == "SHOW COLUMNS FROM `wms`.`t`"
        assert rewrite_for_archery("  DESC t;  ") == "SHOW COLUMNS FROM t"

    def test_other_statements_untouched(self):
        from sqlmux.server import rewrite_for_archery
        for sql in ["SELECT 1", "SHOW TABLES", "EXPLAIN SELECT 1",
                    "SELECT * FROM t ORDER BY id DESC"]:
            assert rewrite_for_archery(sql) == sql


class TestPortBinding:
    """端口与环境固定绑定，不随 --envs 顺序变化（曾致 envs 与 serve 端口不一致）。"""

    def test_port_independent_of_order(self):
        from sqlmux.config import env_targets
        fwd = {t.env: t.port for t in env_targets(["uat-cn", "uat-sea", "pro-cn", "pro-sea"])}
        rev = {t.env: t.port for t in env_targets(["pro-sea", "pro-cn", "uat-sea", "uat-cn"])}
        assert fwd == rev
        assert fwd == {"uat-cn": 13306, "uat-sea": 13307, "pro-cn": 13308, "pro-sea": 13309}

    def test_single_env_keeps_its_port(self):
        from sqlmux.config import env_targets
        got = {t.env: t.port for t in env_targets(["uat-sea"])}
        assert got == {"uat-sea": 13307}
