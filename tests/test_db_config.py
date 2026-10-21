# -*- coding: utf-8 -*-
"""数据库配置和诊断测试；不连接真实 MySQL。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "智能体"))
import db
from sqlalchemy.exc import OperationalError
import pymysql


class TestDatabaseConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = Path(self.tmp.name) / ".env"
        self.env_patch = patch.object(db, "_ENV_PATH", str(self.env_path))
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.process_patch = patch.dict(os.environ, {}, clear=True)
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)
        self.write_env()

    def write_env(self, host="db-service", password="p@ss:/?#%${literal}"):
        self.env_path.write_text(
            f"MYSQL_HOST={host}\nMYSQL_PORT=3306\nMYSQL_USER=app\n"
            f"MYSQL_PASSWORD='{password}'\nMYSQL_DATABASE=customer_ai\n", encoding="utf-8",
        )

    def test_password_special_characters_are_not_interpreted_as_url(self):
        url = db._mysql_url()
        self.assertEqual(url.host, "db-service")
        self.assertEqual(url.password, "p@ss:/?#%${literal}")
        self.assertNotIn(url.password, str(url))
        self.assertNotIn("MYSQL_HOST", os.environ)

    def test_reads_file_changes_without_polluting_process_environment(self):
        self.assertEqual(db._mysql_url().host, "db-service")
        self.write_env(host="corrected-db")
        self.assertEqual(db._mysql_url().host, "corrected-db")
        self.assertNotIn("MYSQL_HOST", os.environ)

    def test_explicit_process_environment_has_priority(self):
        with patch.dict(os.environ, {"MYSQL_HOST": "docker-db"}):
            self.assertEqual(db._mysql_url().host, "docker-db")
            info = db.mysql_connection_info()
            self.assertIn("MYSQL_HOST", info["process_overrides"])
            self.assertNotIn("MYSQL_PASSWORD", info)

    def test_missing_configuration_does_not_silently_use_loopback(self):
        self.env_path.unlink()
        with self.assertRaisesRegex(RuntimeError, "MYSQL_HOST"):
            db._mysql_url()

    def test_configuration_change_disposes_old_pool_and_resets_schema_flag(self):
        engine1 = Mock(url=db._mysql_url())
        self.write_env(host="new-db")
        engine2 = Mock(url=db._mysql_url())
        with patch.object(db, "_ENGINE", engine1), patch.object(db, "_DB_READY", True), \
             patch.object(db, "create_engine", return_value=engine2) as create:
            self.assertIs(db.get_engine(), engine2)
            engine1.dispose.assert_called_once()
            self.assertFalse(db._DB_READY)
            self.assertEqual(create.call_args.args[0].host, "new-db")

    def test_loopback_connection_error_explains_container_networking(self):
        self.write_env(host="127.0.0.1", password="private-password")
        error = OperationalError("SELECT 1", {}, pymysql.err.OperationalError(2003, "connection refused"))
        msg = db.format_database_error(error)
        self.assertIn("Docker", msg)
        self.assertIn(str(self.env_path), msg)
        self.assertNotIn("private-password", msg)

    def test_connection_test_only_executes_select(self):
        engine = Mock()
        conn = engine.connect.return_value.__enter__ = Mock(return_value=Mock())
        engine.connect.return_value.__exit__ = Mock(return_value=False)
        with patch.object(db, "reset_engine") as reset, patch.object(db, "get_engine", return_value=engine):
            db.test_mysql_connection()
            reset.assert_called_once()
            self.assertEqual(str(conn.return_value.execute.call_args.args[0]), "SELECT 1")
