# -*- coding: utf-8 -*-
"""
客户 / 会话 / 聊天记录 / 质检结果 / 成交分析 · MySQL 持久化层
会话 ID 为唯一业务主键。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Callable, TypeVar

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError

import qc_core as core

logger = logging.getLogger(__name__)

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_DIR = (os.environ.get("QC_CONFIG_DIR") or "").strip() or _APP_DIR
_ENV_PATH = os.path.join(_CONFIG_DIR, ".env")

_SESSION_ID_KEYS = ("会话ID", "会话id", "session_id", "sessionId", "Session ID", "会话编号")

_ENGINE: Engine | None = None
_DB_READY = False

_TRANSIENT_MYSQL_CODES = frozenset({2003, 2006, 2013, 2055})
_DB_RETRY_ATTEMPTS = 3
_DB_RETRY_BASE_DELAY = 1.5

T = TypeVar("T")

_DEAL_SIGNALS = (
    "成交", "已付款", "已支付", "已下单", "付款完成", "支付完成", "已付",
)

_CREATE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS customers (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        external_customer_key VARCHAR(255) NOT NULL,
        original_session_id VARCHAR(255) NOT NULL,
        contact_name VARCHAR(255),
        platform VARCHAR(100),
        channel VARCHAR(100),
        sales_name VARCHAR(255),
        status VARCHAR(50) DEFAULT '未成交',
        product_name VARCHAR(255),
        deal_amount DECIMAL(12,2),
        first_message_at DATETIME,
        last_message_at DATETIME,
        deal_at DATETIME,
        deleted_at DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_external_customer_key (external_customer_key),
        UNIQUE KEY uk_original_session_id (original_session_id),
        KEY idx_status (status),
        KEY idx_sales_name (sales_name),
        KEY idx_deal_at (deal_at),
        KEY idx_last_message_at (last_message_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_sessions (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        session_key VARCHAR(255) NOT NULL,
        customer_id BIGINT NOT NULL,
        source_file VARCHAR(255),
        original_session_id VARCHAR(255) NOT NULL,
        contact_name VARCHAR(255),
        sales_name VARCHAR(255),
        channel VARCHAR(100),
        message_count_customer INT DEFAULT 0,
        message_count_sales INT DEFAULT 0,
        total_message_count INT DEFAULT 0,
        first_message_at DATETIME,
        last_message_at DATETIME,
        raw_dialog MEDIUMTEXT,
        imported_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_session_key (session_key),
        UNIQUE KEY uk_original_session_id (original_session_id),
        KEY idx_customer_id (customer_id),
        KEY idx_imported_at (imported_at),
        CONSTRAINT fk_sessions_customer
            FOREIGN KEY (customer_id) REFERENCES customers(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        message_key VARCHAR(255) NOT NULL,
        customer_id BIGINT NOT NULL,
        session_id BIGINT NOT NULL,
        message_order INT NOT NULL,
        sender_type VARCHAR(50),
        sender_name VARCHAR(255),
        content TEXT,
        sent_at DATETIME,
        raw_line TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_message_key (message_key),
        UNIQUE KEY uk_session_message_order (session_id, message_order),
        KEY idx_customer_time (customer_id, sent_at),
        KEY idx_session_order (session_id, message_order),
        KEY idx_sender_type (sender_type),
        CONSTRAINT fk_messages_customer
            FOREIGN KEY (customer_id) REFERENCES customers(id),
        CONSTRAINT fk_messages_session
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS qc_reports (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        customer_id BIGINT NOT NULL,
        session_id BIGINT,
        result_tag VARCHAR(50),
        customer_stage VARCHAR(100),
        customer_objection TEXT,
        is_qualified VARCHAR(50),
        risk_level VARCHAR(50),
        need_human_review VARCHAR(50),
        redline TEXT,
        response_quality TEXT,
        main_problem TEXT,
        improvement_suggestion TEXT,
        next_action TEXT,
        raw_result JSON,
        analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_customer_id (customer_id),
        KEY idx_session_id (session_id),
        KEY idx_result_tag (result_tag),
        KEY idx_analyzed_at (analyzed_at),
        CONSTRAINT fk_qc_customer
            FOREIGN KEY (customer_id) REFERENCES customers(id),
        CONSTRAINT fk_qc_session
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS deal_analysis (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        customer_id BIGINT NOT NULL,
        session_id BIGINT,
        deal_stage_summary TEXT,
        customer_profile TEXT,
        core_need TEXT,
        core_pain_point TEXT,
        main_objection TEXT,
        trust_trigger TEXT,
        deal_trigger TEXT,
        key_turning_points TEXT,
        sales_key_actions TEXT,
        customer_psychology_path TEXT,
        deal_signals TEXT,
        reusable_sales_experience TEXT,
        recommended_agent_rules TEXT,
        raw_result JSON,
        analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_customer_latest_deal_analysis (customer_id),
        KEY idx_session_id (session_id),
        KEY idx_analyzed_at (analyzed_at),
        CONSTRAINT fk_deal_analysis_customer
            FOREIGN KEY (customer_id) REFERENCES customers(id),
        CONSTRAINT fk_deal_analysis_session
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_learning_runs (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        run_date DATE NOT NULL,
        new_deal_customers INT DEFAULT 0,
        analyzed_customers INT DEFAULT 0,
        failed_customers INT DEFAULT 0,
        summary TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_run_date (run_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS delete_analysis (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        customer_id BIGINT NOT NULL,
        session_id BIGINT,
        delete_stage_summary TEXT,
        customer_profile TEXT,
        initial_need TEXT,
        unmet_need TEXT,
        trust_breakdown_point TEXT,
        delete_trigger TEXT,
        sales_mistakes TEXT,
        warning_signals TEXT,
        recoverable TEXT,
        customer_psychology_path TEXT,
        churn_pattern TEXT,
        avoidable_experience TEXT,
        recommended_agent_rules TEXT,
        raw_result JSON,
        analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_customer_latest_delete_analysis (customer_id),
        KEY idx_session_id (session_id),
        KEY idx_analyzed_at (analyzed_at),
        KEY idx_churn_pattern (churn_pattern(64)),
        CONSTRAINT fk_delete_analysis_customer
            FOREIGN KEY (customer_id) REFERENCES customers(id),
        CONSTRAINT fk_delete_analysis_session
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_delete_learning_runs (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        run_date DATE NOT NULL,
        new_delete_customers INT DEFAULT 0,
        analyzed_customers INT DEFAULT 0,
        failed_customers INT DEFAULT 0,
        summary TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_delete_run_date (run_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS import_conflicts (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        original_session_id VARCHAR(255) NOT NULL,
        customer_id BIGINT,
        session_id BIGINT,
        source_file VARCHAR(255),
        last_db_message TEXT,
        new_dialog_preview TEXT,
        reason VARCHAR(255),
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_original_session_id (original_session_id),
        KEY idx_created_at (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]


def load_mysql_settings() -> dict:
    """每次读取当前配置，进程环境变量优先于指定配置目录中的 .env；不污染 os.environ。"""
    values = dotenv_values(_ENV_PATH, interpolate=False) if os.path.isfile(_ENV_PATH) else {}
    defaults = {"MYSQL_PORT": "3306", "MYSQL_CHARSET": "utf8mb4"}
    return {
        key: str(os.environ.get(key, values.get(key) or defaults.get(key, "")))
        for key in ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE", "MYSQL_CHARSET")
    }


def mysql_connection_info() -> dict:
    """可展示的连接目标和配置来源，不含用户名、密码。"""
    cfg = load_mysql_settings()
    return {
        "host": cfg["MYSQL_HOST"], "port": cfg["MYSQL_PORT"],
        "database": cfg["MYSQL_DATABASE"], "env_path": _ENV_PATH,
        "env_exists": os.path.isfile(_ENV_PATH),
        "in_container": os.path.isfile("/.dockerenv"),
        "process_overrides": [key for key in cfg if key in os.environ],
    }


def _mysql_url() -> URL:
    cfg = load_mysql_settings()
    missing = [key for key in ("MYSQL_HOST", "MYSQL_USER", "MYSQL_DATABASE") if not cfg[key].strip()]
    if missing:
        raise RuntimeError(
            f"MySQL 配置缺少 {', '.join(missing)}。请在 {_ENV_PATH} 或容器环境变量中填写；"
            "不会自动使用 127.0.0.1。"
        )
    try:
        port = int(cfg["MYSQL_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise RuntimeError("MYSQL_PORT 必须为 1 至 65535 的整数。") from None
    return URL.create(
        "mysql+pymysql", username=cfg["MYSQL_USER"], password=cfg["MYSQL_PASSWORD"],
        host=cfg["MYSQL_HOST"].strip(), port=port, database=cfg["MYSQL_DATABASE"].strip(),
        query={"charset": cfg["MYSQL_CHARSET"] or "utf8mb4"},
    )


def format_database_error(exc: BaseException) -> str:
    """把常见连接故障转换成不泄露账号密码的可操作提示。"""
    info = mysql_connection_info()
    target = f"{info['host'] or '未配置主机'}:{info['port']}/{info['database'] or '未配置库名'}"
    orig = getattr(exc, "orig", exc)
    args = getattr(orig, "args", ())
    code = args[0] if args and isinstance(args[0], int) else None
    if code in (2003, 2006, 2013, 2055) or isinstance(exc, OSError):
        msg = f"无法连接 MySQL：{target}。请确认数据库已启动、主机和端口正确且应用可访问。"
        if info["host"] in ("127.0.0.1", "localhost", "::1"):
            msg += (
                "如果网页在 Docker 容器中，这个地址指向网页容器自身。"
                "MySQL 在另一容器时填写同一 Docker 网络中的 MySQL 服务名；"
                "在宿主机时填写容器可访问的宿主机地址。"
            )
    elif code == 1045:
        msg = f"MySQL 拒绝登录：{target}。请核对账号密码及该账号对网页所在主机的访问授权。"
    elif code == 1049:
        msg = f"MySQL 数据库不存在：{info['database']}。请核对 MYSQL_DATABASE。"
    elif isinstance(exc, RuntimeError):
        msg = str(exc)
    else:
        msg = f"数据库操作失败：{target}（MySQL 错误码：{code or '未知'}）。请查看服务器日志。"
    return f"{msg} 配置文件：{info['env_path']}；进程 MYSQL_* 环境变量优先。"


def test_mysql_connection() -> dict:
    """重建连接池并执行只读 SELECT 1，不建表、不修改业务数据。"""
    reset_engine()
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise RuntimeError(format_database_error(exc)) from None
    return mysql_connection_info()


def _is_transient_db_error(exc: BaseException) -> bool:
    """判断是否为可重试的 MySQL 连接类错误。"""
    if isinstance(exc, OSError):
        return True
    orig = getattr(exc, "orig", exc)
    args = getattr(orig, "args", ())
    if args and isinstance(args[0], int):
        return args[0] in _TRANSIENT_MYSQL_CODES
    msg = str(exc).lower()
    return (
        "lost connection" in msg
        or "can't connect" in msg
        or "connection refused" in msg
        or "server has gone away" in msg
    )


def reset_engine() -> None:
    """丢弃连接池（连接断开时用于重建）。"""
    global _ENGINE, _DB_READY
    _DB_READY = False
    if _ENGINE is not None:
        try:
            _ENGINE.dispose()
        except Exception:
            logger.exception("dispose engine failed")
        _ENGINE = None


def with_db_retry(fn: Callable[..., T], *args, **kwargs) -> T:
    """对短暂断连执行有限次重试。"""
    last_exc: BaseException | None = None
    for attempt in range(1, _DB_RETRY_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except (SQLAlchemyError, OSError) as e:
            last_exc = e
            if not _is_transient_db_error(e) or attempt >= _DB_RETRY_ATTEMPTS:
                raise
            logger.warning(
                "MySQL 短暂断连，%s/%s 秒后重试：%s",
                attempt,
                _DB_RETRY_ATTEMPTS,
                e,
            )
            reset_engine()
            time.sleep(_DB_RETRY_BASE_DELAY * attempt)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("with_db_retry: unexpected state")


def get_engine() -> Engine:
    global _ENGINE
    url = _mysql_url()
    if _ENGINE is not None and _ENGINE.url != url:
        reset_engine()
    if _ENGINE is None:
        try:
            _ENGINE = create_engine(
                url,
                pool_pre_ping=True, hide_parameters=True,
                pool_recycle=1800,
                pool_size=5,
                max_overflow=10,
                connect_args={
                    "connect_timeout": 8,
                    "read_timeout": 120,
                    "write_timeout": 120,
                },
                json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False),
            )
        except Exception as e:
            raise RuntimeError(format_database_error(e)) from None
    return _ENGINE


def init_db() -> None:
    """自动创建全部业务表（进程内只执行一次 DDL）。"""
    global _DB_READY
    get_engine()  # 检测配置变更，必要时重置旧连接池与建表标记。
    if _DB_READY:
        return

    def _run() -> None:
        engine = get_engine()
        with engine.begin() as conn:
            for ddl in _CREATE_TABLES_SQL:
                conn.execute(text(ddl))
            _run_schema_migrations(conn)

    with_db_retry(_run)
    _DB_READY = True


def _run_schema_migrations(conn) -> None:
    """轻量迁移：为已有库补充新列。"""
    try:
        row = conn.execute(
            text("""
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'customers'
                  AND COLUMN_NAME = 'deleted_at'
            """),
        ).scalar()
        if not row:
            conn.execute(text("ALTER TABLE customers ADD COLUMN deleted_at DATETIME NULL"))
    except Exception:
        logger.exception("schema migration customers.deleted_at failed")


# ---------------------------------------------------------------------------
# 文本 / 键生成
# ---------------------------------------------------------------------------

def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_session_id(session: dict) -> str:
    for key in _SESSION_ID_KEYS:
        value = session.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ValueError("缺少会话ID，无法确定客户唯一身份")


def make_customer_key(session: dict) -> str:
    raw_session_id = normalize_session_id(session)
    raw = f"customer_by_session_id|{raw_session_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_session_key(session: dict) -> str:
    raw_session_id = normalize_session_id(session)
    raw = f"chat_session|{raw_session_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_message_key(session_key: str, message_order: int, sender_name: str, content: str) -> str:
    raw = f"{session_key}|{message_order}|{sender_name or ''}|{content or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sender_type(who: str | None) -> str:
    if not who:
        return "unknown"
    if who == "客户":
        return "customer"
    if core._is_service_speaker(who):  # noqa: SLF001
        return "sales"
    return "unknown"


def parse_dialog_messages(dialog_text: str) -> list[dict]:
    """将完整对话文本拆分为消息列表。"""
    messages = []
    for raw_line in str(dialog_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        who = core._parse_speaker(line)  # noqa: SLF001
        content = core._extract_message_content_from_line(line)  # noqa: SLF001
        if content is None:
            m = re.match(r"^(.+?)\s*[:：]\s*(.+)$", line, re.DOTALL)
            if m:
                who = who or m.group(1).strip()
                content = m.group(2).strip()
            else:
                content = line
        content = normalize_text(content)
        if not content:
            continue
        sent_at = core._parse_line_timestamp(line)  # noqa: SLF001
        messages.append({
            "sender_type": _sender_type(who),
            "sender_name": who or "",
            "content": content,
            "sent_at": sent_at,
            "raw_line": raw_line,
        })
    return messages


def _session_dialog(session: dict) -> str:
    return str(session.get("对话_全量") or session.get("对话") or "")


def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# 客户 / 会话 CRUD
# ---------------------------------------------------------------------------

def get_or_create_customer_by_session_id(session: dict) -> dict:
    """按会话 ID 获取或创建客户，返回含 id 的字典及 created 标记。"""
    original_session_id = normalize_session_id(session)
    customer_key = make_customer_key(session)
    engine = get_engine()

    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT * FROM customers
                WHERE original_session_id = :sid OR external_customer_key = :key
                LIMIT 1
            """),
            {"sid": original_session_id, "key": customer_key},
        ).fetchone()
        if row:
            d = _row_to_dict(row)
            d["created"] = False
            return d

        contact = session.get("联系人") or session.get("contact_name") or ""
        channel = session.get("渠道") or session.get("channel") or ""
        sales = session.get("接待成员") or session.get("sales_name") or ""

        conn.execute(
            text("""
                INSERT IGNORE INTO customers (
                    external_customer_key, original_session_id,
                    contact_name, channel, sales_name, status
                ) VALUES (
                    :key, :sid, :contact, :channel, :sales, '未成交'
                )
            """),
            {
                "key": customer_key,
                "sid": original_session_id,
                "contact": contact or None,
                "channel": channel or None,
                "sales": sales or None,
            },
        )
        row = conn.execute(
            text("""
                SELECT * FROM customers
                WHERE original_session_id = :sid OR external_customer_key = :key
                LIMIT 1
            """),
            {"sid": original_session_id, "key": customer_key},
        ).fetchone()
        d = _row_to_dict(row)
        d["created"] = True
        return d


def get_or_create_chat_session(
    customer_id: int,
    session: dict,
    source_file: str | None = None,
) -> dict:
    original_session_id = normalize_session_id(session)
    session_key = make_session_key(session)
    engine = get_engine()

    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT * FROM chat_sessions
                WHERE original_session_id = :sid OR session_key = :skey
                LIMIT 1
            """),
            {"sid": original_session_id, "skey": session_key},
        ).fetchone()
        if row:
            d = _row_to_dict(row)
            d["created"] = False
            return d

        contact = session.get("联系人") or ""
        sales = session.get("接待成员") or ""
        channel = session.get("渠道") or ""
        dialog = _session_dialog(session)

        conn.execute(
            text("""
                INSERT IGNORE INTO chat_sessions (
                    session_key, customer_id, source_file, original_session_id,
                    contact_name, sales_name, channel, raw_dialog
                ) VALUES (
                    :skey, :cid, :src, :sid, :contact, :sales, :channel, :dialog
                )
            """),
            {
                "skey": session_key,
                "cid": customer_id,
                "src": source_file,
                "sid": original_session_id,
                "contact": contact or None,
                "sales": sales or None,
                "channel": channel or None,
                "dialog": dialog or None,
            },
        )
        row = conn.execute(
            text("""
                SELECT * FROM chat_sessions
                WHERE original_session_id = :sid OR session_key = :skey
                LIMIT 1
            """),
            {"sid": original_session_id, "skey": session_key},
        ).fetchone()
        d = _row_to_dict(row)
        d["created"] = True
        return d


def get_session_messages(session_db_id: int) -> list[dict]:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT * FROM chat_messages
                WHERE session_id = :sid
                ORDER BY message_order ASC
            """),
            {"sid": session_db_id},
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_last_message(session_db_id: int) -> dict | None:
    messages = get_session_messages(session_db_id)
    return messages[-1] if messages else None


def find_message_position_in_list(messages: list[dict], target_message: dict) -> int:
    target_content = normalize_text(target_message.get("content"))
    target_sender = normalize_text(target_message.get("sender_name"))

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        msg_content = normalize_text(msg.get("content"))
        msg_sender = normalize_text(msg.get("sender_name"))

        if not msg_content:
            continue

        if msg_content == target_content:
            if not target_sender or not msg_sender or target_sender == msg_sender:
                return i

    return -1


def find_last_message_position(new_messages: list[dict], last_db_message: dict) -> int:
    return find_message_position_in_list(new_messages, last_db_message)


def record_import_conflict(
    original_session_id: str,
    customer_id: int | None,
    session_id: int | None,
    source_file: str | None,
    last_db_message: dict | None,
    new_dialog_preview: str,
    reason: str,
) -> None:
    engine = get_engine()
    last_msg_text = ""
    if last_db_message:
        last_msg_text = json.dumps(
            {
                "content": last_db_message.get("content"),
                "sender_name": last_db_message.get("sender_name"),
                "message_order": last_db_message.get("message_order"),
            },
            ensure_ascii=False,
        )
    preview = (new_dialog_preview or "")[:2000]
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO import_conflicts (
                    original_session_id, customer_id, session_id,
                    source_file, last_db_message, new_dialog_preview, reason
                ) VALUES (
                    :sid, :cid, :sess_id, :src, :last_msg, :preview, :reason
                )
            """),
            {
                "sid": original_session_id,
                "cid": customer_id,
                "sess_id": session_id,
                "src": source_file,
                "last_msg": last_msg_text,
                "preview": preview,
                "reason": reason,
            },
        )


def _update_session_and_customer_stats(
    conn,
    customer_id: int,
    session_db_id: int,
    session: dict,
    raw_dialog: str,
) -> None:
    cust_n, sales_n = core.count_roles(raw_dialog)
    total = cust_n + sales_n
    messages = parse_dialog_messages(raw_dialog)
    first_at = None
    last_at = None
    for m in messages:
        ts = m.get("sent_at")
        if ts:
            if first_at is None or ts < first_at:
                first_at = ts
            if last_at is None or ts > last_at:
                last_at = ts

    conn.execute(
        text("""
            UPDATE chat_sessions SET
                message_count_customer = :cust_n,
                message_count_sales = :sales_n,
                total_message_count = :total,
                first_message_at = COALESCE(:first_at, first_message_at),
                last_message_at = :last_at,
                raw_dialog = :dialog,
                contact_name = COALESCE(:contact, contact_name),
                sales_name = COALESCE(:sales, sales_name),
                channel = COALESCE(:channel, channel),
                updated_at = NOW()
            WHERE id = :sid
        """),
        {
            "cust_n": cust_n,
            "sales_n": sales_n,
            "total": total,
            "first_at": first_at,
            "last_at": last_at,
            "dialog": raw_dialog,
            "contact": session.get("联系人") or None,
            "sales": session.get("接待成员") or None,
            "channel": session.get("渠道") or None,
            "sid": session_db_id,
        },
    )
    conn.execute(
        text("""
            UPDATE customers SET
                contact_name = COALESCE(:contact, contact_name),
                sales_name = COALESCE(:sales, sales_name),
                channel = COALESCE(:channel, channel),
                first_message_at = COALESCE(:first_at, first_message_at),
                last_message_at = :last_at,
                updated_at = NOW()
            WHERE id = :cid
        """),
        {
            "contact": session.get("联系人") or None,
            "sales": session.get("接待成员") or None,
            "channel": session.get("渠道") or None,
            "first_at": first_at,
            "last_at": last_at,
            "cid": customer_id,
        },
    )


def append_new_messages_by_last_line(
    customer_id: int,
    session_db_id: int,
    session_key: str,
    original_session_id: str,
    new_messages: list[dict],
    source_file: str | None = None,
    session: dict | None = None,
    raw_dialog: str | None = None,
) -> dict:
    """
    按会话增量合并聊天，仅追加新记录中多出来的消息：
    1. 老记录最后一条在新记录中 → 追加其后新增部分
    2. 新记录最后一条在老记录中 → 跳过
    3. 两边最后一条对不上 → 跳过
    """
    if not new_messages:
        return {"inserted_messages": 0, "skipped": False, "reason": "聊天记录为空"}

    db_messages = get_session_messages(session_db_id)
    last_db_message = db_messages[-1] if db_messages else None
    new_last_message = new_messages[-1]

    if last_db_message is not None:
        if find_message_position_in_list(db_messages, new_last_message) >= 0:
            return {
                "inserted_messages": 0,
                "skipped": True,
                "reason": "新记录最后一条已在老记录中，已跳过",
            }

        anchor = find_message_position_in_list(new_messages, last_db_message)
        if anchor == -1:
            return {
                "inserted_messages": 0,
                "skipped": True,
                "reason": "两边最后一条对不上，已跳过",
            }

        tail = new_messages[anchor + 1:]
        if not tail:
            return {
                "inserted_messages": 0,
                "skipped": True,
                "reason": "无新增聊天",
            }
        start_order = last_db_message["message_order"]
    else:
        tail = new_messages
        start_order = 0

    inserted = 0
    engine = get_engine()
    with engine.begin() as conn:
        for offset, msg in enumerate(tail, start=1):
            message_order = start_order + offset
            mkey = make_message_key(
                session_key, message_order, msg.get("sender_name"), msg.get("content"),
            )
            result = conn.execute(
                text("""
                    INSERT IGNORE INTO chat_messages (
                        message_key, customer_id, session_id, message_order,
                        sender_type, sender_name, content, sent_at, raw_line
                    ) VALUES (
                        :mkey, :cid, :sid, :ord, :stype, :sname, :content, :sent, :raw
                    )
                """),
                {
                    "mkey": mkey,
                    "cid": customer_id,
                    "sid": session_db_id,
                    "ord": message_order,
                    "stype": msg.get("sender_type"),
                    "sname": msg.get("sender_name") or None,
                    "content": msg.get("content"),
                    "sent": msg.get("sent_at"),
                    "raw": msg.get("raw_line"),
                },
            )
            if result.rowcount:
                inserted += 1

        if inserted and session and raw_dialog is not None:
            _update_session_and_customer_stats(
                conn, customer_id, session_db_id, session, raw_dialog,
            )
        elif inserted and source_file:
            conn.execute(
                text("UPDATE chat_sessions SET source_file = :src WHERE id = :sid"),
                {"src": source_file, "sid": session_db_id},
            )

    return {"inserted_messages": inserted, "skipped": False, "reason": ""}


def upsert_sessions(sessions: list[dict], source_file: str | None = None) -> dict:
    """批量入库会话；以会话 ID 为唯一键，增量追加聊天。"""
    init_db()
    result = {
        "customers_created": 0,
        "customers_updated": 0,
        "sessions_created": 0,
        "sessions_updated": 0,
        "messages_inserted": 0,
        "unchanged_sessions": 0,
        "conflicts": 0,
        "conflict_session_ids": [],
        "skipped_no_session_id": 0,
        "errors": [],
        "session_details": [],
    }

    for session in sessions:
        try:
            original_session_id = normalize_session_id(session)
        except ValueError as e:
            result["skipped_no_session_id"] += 1
            err_msg = str(e)
            result["errors"].append(err_msg)
            result["session_details"].append({
                "session_id": session.get("会话ID") or "",
                "contact_name": session.get("联系人") or "",
                "source_file": source_file or "",
                "status": "失败",
                "reason": err_msg,
                "messages_inserted": 0,
            })
            continue

        try:
            customer = get_or_create_customer_by_session_id(session)
            if customer.get("created"):
                result["customers_created"] += 1
            else:
                result["customers_updated"] += 1

            chat_sess = get_or_create_chat_session(
                customer["id"], session, source_file=source_file,
            )
            if chat_sess.get("created"):
                result["sessions_created"] += 1
            else:
                result["sessions_updated"] += 1

            dialog = _session_dialog(session)
            new_messages = parse_dialog_messages(dialog)
            session_key = make_session_key(session)

            append_result = append_new_messages_by_last_line(
                customer_id=customer["id"],
                session_db_id=chat_sess["id"],
                session_key=session_key,
                original_session_id=original_session_id,
                new_messages=new_messages,
                source_file=source_file,
                session=session,
                raw_dialog=dialog,
            )

            inserted = append_result.get("inserted_messages", 0)
            result["messages_inserted"] += inserted

            detail = {
                "session_id": original_session_id,
                "contact_name": session.get("联系人") or "",
                "source_file": source_file or "",
                "status": "成功",
                "reason": "",
                "messages_inserted": inserted,
                "is_new_customer": bool(customer.get("created")),
                "is_new_session": bool(chat_sess.get("created")),
            }
            skip_reason = append_result.get("reason") or ""
            if skip_reason == "聊天记录为空":
                detail["status"] = "失败"
                detail["reason"] = skip_reason
            elif append_result.get("skipped") or skip_reason in (
                "无新增聊天",
                "新记录最后一条已在老记录中，已跳过",
                "两边最后一条对不上，已跳过",
            ):
                detail["status"] = "跳过"
                detail["reason"] = skip_reason or "已跳过"
                result["unchanged_sessions"] += 1
            result["session_details"].append(detail)

        except SQLAlchemyError as e:
            logger.exception("upsert session %s failed", original_session_id)
            err_msg = f"会话 {original_session_id}: {e}"
            result["errors"].append(err_msg)
            result["session_details"].append({
                "session_id": original_session_id,
                "contact_name": session.get("联系人") or "",
                "source_file": source_file or "",
                "status": "失败",
                "reason": str(e),
                "messages_inserted": 0,
            })
        except Exception as e:
            logger.exception("upsert session %s failed", original_session_id)
            err_msg = f"会话 {original_session_id}: {e}"
            result["errors"].append(err_msg)
            result["session_details"].append({
                "session_id": original_session_id,
                "contact_name": session.get("联系人") or "",
                "source_file": source_file or "",
                "status": "失败",
                "reason": str(e),
                "messages_inserted": 0,
            })

    return result


# ---------------------------------------------------------------------------
# 质检结果 / 成交识别
# ---------------------------------------------------------------------------

def _resolve_session_by_result(result: dict) -> dict | None:
    try:
        sid = normalize_session_id(result)
    except ValueError:
        return None
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT s.id AS session_id, s.customer_id, s.original_session_id
                FROM chat_sessions s
                WHERE s.original_session_id = :sid
                LIMIT 1
            """),
            {"sid": sid},
        ).fetchone()
        return _row_to_dict(row)


def _is_deal_from_qc(result: dict) -> bool:
    tag = str(result.get("结果标签") or result.get("result_tag") or "").strip()
    if tag == "成交":
        return True
    stage = str(result.get("客户阶段") or result.get("customer_stage") or "")
    for kw in ("成交", "已付款", "已下单"):
        if kw in stage:
            return True
    blob = " ".join(
        str(result.get(k, "")) for k in ("原始对话", "客户阶段", "结果标签", "下一步跟进动作")
    )
    for sig in _DEAL_SIGNALS:
        if sig in blob:
            return True
    return False


def save_qc_results(results: list[dict]) -> dict:
    init_db()
    saved = 0
    skipped = 0
    engine = get_engine()

    with engine.begin() as conn:
        for result in results:
            resolved = _resolve_session_by_result(result)
            if not resolved:
                skipped += 1
                continue

            raw = {k: v for k, v in result.items() if not str(k).startswith("_")}
            conn.execute(
                text("""
                    INSERT INTO qc_reports (
                        customer_id, session_id, result_tag, customer_stage,
                        customer_objection, is_qualified, risk_level,
                        need_human_review, redline, response_quality,
                        main_problem, improvement_suggestion, next_action, raw_result
                    ) VALUES (
                        :cid, :sid, :tag, :stage, :objection, :qualified,
                        :risk, :review, :redline, :quality, :problem,
                        :suggestion, :action, :raw
                    )
                """),
                {
                    "cid": resolved["customer_id"],
                    "sid": resolved["session_id"],
                    "tag": result.get("结果标签") or result.get("result_tag"),
                    "stage": result.get("客户阶段") or result.get("customer_stage"),
                    "objection": result.get("客户主要顾虑") or result.get("customer_objection"),
                    "qualified": result.get("是否合格") or result.get("is_qualified"),
                    "risk": result.get("风险等级") or result.get("risk_level"),
                    "review": result.get("是否需要人工复核") or result.get("need_human_review"),
                    "redline": result.get("红线") or result.get("redline"),
                    "quality": result.get("承接评价") or result.get("response_quality"),
                    "problem": result.get("主要问题") or result.get("main_problem"),
                    "suggestion": result.get("改进建议") or result.get("improvement_suggestion"),
                    "action": result.get("下一步跟进动作") or result.get("next_action"),
                    "raw": json.dumps(raw, ensure_ascii=False, default=str),
                },
            )
            saved += 1

    return {"saved": saved, "skipped": skipped}


def mark_deal_customers_from_qc(results: list[dict]) -> dict:
    init_db()
    deal_customers = 0
    engine = get_engine()

    with engine.begin() as conn:
        for result in results:
            if not _is_deal_from_qc(result):
                continue
            resolved = _resolve_session_by_result(result)
            if not resolved:
                continue
            conn.execute(
                text("""
                    UPDATE customers
                    SET status = '已成交',
                        deal_at = COALESCE(deal_at, NOW()),
                        updated_at = NOW()
                    WHERE id = :cid
                """),
                {"cid": resolved["customer_id"]},
            )
            deal_customers += 1

    return {"deal_customers": deal_customers}


# ---------------------------------------------------------------------------
# 成交心理分析
# ---------------------------------------------------------------------------

def _pending_learning_query(kind: str, *, count: bool = False) -> str:
    if kind not in ("deal", "delete"):
        raise ValueError("invalid learning type")
    status = "已成交" if kind == "deal" else "已删除"
    event_at = "deal_at" if kind == "deal" else "deleted_at"
    # 表名仅取上方白名单；继承分析需重做，但不会删除原结果。
    repair = "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(da.raw_result, '$.\"_学习模式\"')), '') = '继承跳过'"
    fields = "COUNT(*)" if count else f"""c.id AS customer_id,
        (SELECT s.id FROM chat_sessions s WHERE s.customer_id=c.id
         ORDER BY s.updated_at DESC, s.id DESC LIMIT 1) AS session_id,
        c.original_session_id, c.contact_name, c.sales_name, c.status, c.{event_at},
        CASE WHEN da.id IS NOT NULL AND {repair} THEN 1 ELSE 0 END AS needs_repair"""
    sql = f"""SELECT {fields} FROM customers c
        LEFT JOIN {kind}_analysis da ON da.customer_id=c.id
        WHERE c.status='{status}' AND (
            da.id IS NULL OR TRIM(COALESCE(da.recommended_agent_rules,''))=''
            OR {repair}
            OR EXISTS (SELECT 1 FROM chat_sessions s WHERE s.customer_id=c.id
                       AND s.updated_at > da.analyzed_at)
        )"""
    if kind == "delete":
        sql += " AND EXISTS (SELECT 1 FROM chat_sessions s WHERE s.customer_id=c.id AND s.message_count_customer > 6)"
    if not count:
        sql += f" ORDER BY needs_repair ASC, c.{event_at} DESC, c.id DESC LIMIT :lim"
    return sql


def _count_pending_learning(kind: str) -> int:
    init_db()
    with get_engine().connect() as conn:
        return int(conn.execute(text(_pending_learning_query(kind, count=True))).scalar() or 0)


def _get_pending_learning(kind: str, limit: int) -> list[dict]:
    init_db()
    with get_engine().connect() as conn:
        rows = conn.execute(text(_pending_learning_query(kind)), {"lim": max(1, int(limit))}).fetchall()
        return [_row_to_dict(r) for r in rows]


def count_unanalyzed_deal_customers() -> int:
    return _count_pending_learning("deal")


def get_unanalyzed_deal_customers(limit: int = 20) -> list[dict]:
    return _get_pending_learning("deal", limit)


def get_customer_full_dialog(customer_id: int) -> str:
    def _run() -> str:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT sender_type, sender_name, content, sent_at, raw_line
                    FROM chat_messages
                    WHERE customer_id = :cid
                    ORDER BY message_order ASC
                """),
                {"cid": customer_id},
            ).fetchall()

        if not rows:
            with engine.connect() as conn:
                row = conn.execute(
                    text("""
                        SELECT raw_dialog FROM chat_sessions
                        WHERE customer_id = :cid
                        ORDER BY updated_at DESC LIMIT 1
                    """),
                    {"cid": customer_id},
                ).fetchone()
                if row and row.raw_dialog:
                    return str(row.raw_dialog)
            return ""

        lines = []
        for r in rows:
            d = _row_to_dict(r)
            if d.get("raw_line"):
                lines.append(d["raw_line"])
                continue
            ts = d.get("sent_at")
            sender = d.get("sender_name") or "未知"
            content = d.get("content") or ""
            if ts:
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, datetime) else str(ts)
                lines.append(f"[{ts_str}] {sender}：{content}")
            else:
                lines.append(f"{sender}：{content}")
        return "\n".join(lines)

    return with_db_retry(_run)


_DEAL_FIELD_MAP = {
    "成交阶段总结": "deal_stage_summary",
    "客户画像": "customer_profile",
    "核心需求": "core_need",
    "核心痛点": "core_pain_point",
    "主要顾虑": "main_objection",
    "信任触发点": "trust_trigger",
    "成交触发点": "deal_trigger",
    "关键转折点": "key_turning_points",
    "销售关键动作": "sales_key_actions",
    "客户心理路径": "customer_psychology_path",
    "成交信号": "deal_signals",
    "可复用销售经验": "reusable_sales_experience",
    "智能体判断规则": "recommended_agent_rules",
}


def analysis_text(value) -> str:
    """AI文本字段也可能返回数组/对象，转换为无损JSON后再绑定到TEXT列。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value).strip()


def save_deal_analysis(customer_id: int, session_id: int | None, analysis: dict) -> None:
    init_db()
    fields = {db_col: analysis_text(analysis.get(cn_key) or analysis.get(db_col) or "")
              for cn_key, db_col in _DEAL_FIELD_MAP.items()}
    raw = analysis if isinstance(analysis, dict) else {}

    def _run() -> None:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO deal_analysis (
                        customer_id, session_id,
                        deal_stage_summary, customer_profile, core_need,
                        core_pain_point, main_objection, trust_trigger,
                        deal_trigger, key_turning_points, sales_key_actions,
                        customer_psychology_path, deal_signals,
                        reusable_sales_experience, recommended_agent_rules,
                        raw_result
                    ) VALUES (
                        :cid, :sid,
                        :deal_stage_summary, :customer_profile, :core_need,
                        :core_pain_point, :main_objection, :trust_trigger,
                        :deal_trigger, :key_turning_points, :sales_key_actions,
                        :customer_psychology_path, :deal_signals,
                        :reusable_sales_experience, :recommended_agent_rules,
                        :raw
                    )
                    ON DUPLICATE KEY UPDATE
                        session_id = VALUES(session_id),
                        deal_stage_summary = VALUES(deal_stage_summary),
                        customer_profile = VALUES(customer_profile),
                        core_need = VALUES(core_need),
                        core_pain_point = VALUES(core_pain_point),
                        main_objection = VALUES(main_objection),
                        trust_trigger = VALUES(trust_trigger),
                        deal_trigger = VALUES(deal_trigger),
                        key_turning_points = VALUES(key_turning_points),
                        sales_key_actions = VALUES(sales_key_actions),
                        customer_psychology_path = VALUES(customer_psychology_path),
                        deal_signals = VALUES(deal_signals),
                        reusable_sales_experience = VALUES(reusable_sales_experience),
                        recommended_agent_rules = VALUES(recommended_agent_rules),
                        raw_result = VALUES(raw_result),
                        analyzed_at = NOW()
                """),
                {
                    "cid": customer_id,
                    "sid": session_id,
                    **fields,
                    "raw": json.dumps(raw, ensure_ascii=False, default=str),
                },
            )

    with_db_retry(_run)


def list_deal_analyses(limit: int = 200, since_date: datetime | None = None) -> list[dict]:
    """列出成交心理分析记录（含客户信息），供质检参考检索。"""
    init_db()
    engine = get_engine()
    sql = """
        SELECT da.*,
               c.contact_name,
               c.original_session_id,
               c.sales_name,
               c.status AS customer_status
        FROM deal_analysis da
        JOIN customers c ON c.id = da.customer_id
        WHERE 1=1
    """
    params: dict[str, Any] = {"lim": int(limit)}
    if since_date is not None:
        sql += " AND da.analyzed_at >= :since"
        params["since"] = since_date
    sql += " ORDER BY da.analyzed_at DESC LIMIT :lim"
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_deal_analyses_since_days(days: int = 7, limit: int = 500) -> list[dict]:
    """最近 N 天内新增的成交分析。"""
    from datetime import timedelta

    since = datetime.now() - timedelta(days=max(1, int(days)))
    return list_deal_analyses(limit=limit, since_date=since)


def list_distinct_agent_rules() -> list[str]:
    """deal_analysis 中不重复且非空的「智能体判断规则」文本列表。"""
    init_db()
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT recommended_agent_rules
                FROM deal_analysis
                WHERE recommended_agent_rules IS NOT NULL
                  AND TRIM(recommended_agent_rules) != ''
            """),
        ).fetchall()
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        rule = str(row[0] or "").strip()
        if rule and rule not in seen:
            seen.add(rule)
            out.append(rule)
    return out


def save_daily_learning_run(
    run_date,
    new_deal_customers: int,
    analyzed_customers: int,
    failed_customers: int,
    summary: str,
) -> None:
    init_db()

    def _run() -> None:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO daily_learning_runs (
                        run_date, new_deal_customers, analyzed_customers,
                        failed_customers, summary
                    ) VALUES (
                        :dt, :new_deal, :analyzed, :failed, :summary
                    )
                    ON DUPLICATE KEY UPDATE
                        new_deal_customers = VALUES(new_deal_customers),
                        analyzed_customers = VALUES(analyzed_customers),
                        failed_customers = VALUES(failed_customers),
                        summary = VALUES(summary)
                """),
                {
                    "dt": run_date,
                    "new_deal": new_deal_customers,
                    "analyzed": analyzed_customers,
                    "failed": failed_customers,
                    "summary": summary,
                },
            )

    with_db_retry(_run)


# ---------------------------------------------------------------------------
# 删除客户标记与心理分析
# ---------------------------------------------------------------------------

def mark_sessions_as_deleted(sessions: list[dict]) -> int:
    """将会话对应客户标记为已删除（不覆盖已成交）。"""
    init_db()
    engine = get_engine()
    marked = 0
    with engine.begin() as conn:
        for session in sessions:
            try:
                sid = normalize_session_id(session)
            except ValueError:
                continue
            result = conn.execute(
                text("""
                    UPDATE customers c
                    JOIN chat_sessions s ON s.customer_id = c.id
                    SET c.status = '已删除',
                        c.deleted_at = COALESCE(c.deleted_at, NOW()),
                        c.updated_at = NOW()
                    WHERE s.original_session_id = :sid
                      AND c.status != '已成交'
                """),
                {"sid": sid},
            )
            marked += result.rowcount or 0
    return marked


def _is_delete_from_qc(result: dict) -> bool:
    tag = str(result.get("结果标签") or result.get("result_tag") or "").strip()
    if tag == "删":
        return True
    stage = str(result.get("客户阶段") or result.get("customer_stage") or "")
    return "删" in stage or "拉黑" in stage or "拒收" in stage


def mark_deleted_customers_from_qc(results: list[dict]) -> dict:
    """从质检结果标记删除客户（排除黑粉）。"""
    init_db()
    deleted_customers = 0
    engine = get_engine()

    with engine.begin() as conn:
        for result in results:
            if str(result.get("结果标签") or "").strip() == "黑粉":
                continue
            if not _is_delete_from_qc(result):
                continue
            resolved = _resolve_session_by_result(result)
            if not resolved:
                continue
            conn.execute(
                text("""
                    UPDATE customers
                    SET status = '已删除',
                        deleted_at = COALESCE(deleted_at, NOW()),
                        updated_at = NOW()
                    WHERE id = :cid
                      AND status != '已成交'
                """),
                {"cid": resolved["customer_id"]},
            )
            deleted_customers += 1

    return {"deleted_customers": deleted_customers}


def count_unanalyzed_delete_customers() -> int:
    return _count_pending_learning("delete")


def get_unanalyzed_delete_customers(limit: int = 20) -> list[dict]:
    return _get_pending_learning("delete", limit)


_DELETE_FIELD_MAP = {
    "删除阶段总结": "delete_stage_summary",
    "客户画像": "customer_profile",
    "初始需求": "initial_need",
    "未满足的需求": "unmet_need",
    "信任崩塌点": "trust_breakdown_point",
    "删除触发点": "delete_trigger",
    "销售踩雷动作": "sales_mistakes",
    "删除前预警信号": "warning_signals",
    "是否可挽回": "recoverable",
    "客户心理路径": "customer_psychology_path",
    "流失模式分类": "churn_pattern",
    "可规避销售经验": "avoidable_experience",
    "智能体判断规则": "recommended_agent_rules",
}


def save_delete_analysis(customer_id: int, session_id: int | None, analysis: dict) -> None:
    init_db()
    fields = {db_col: analysis_text(analysis.get(cn_key) or analysis.get(db_col) or "")
              for cn_key, db_col in _DELETE_FIELD_MAP.items()}
    raw = analysis if isinstance(analysis, dict) else {}

    def _run() -> None:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO delete_analysis (
                        customer_id, session_id,
                        delete_stage_summary, customer_profile, initial_need,
                        unmet_need, trust_breakdown_point, delete_trigger,
                        sales_mistakes, warning_signals, recoverable,
                        customer_psychology_path, churn_pattern,
                        avoidable_experience, recommended_agent_rules,
                        raw_result
                    ) VALUES (
                        :cid, :sid,
                        :delete_stage_summary, :customer_profile, :initial_need,
                        :unmet_need, :trust_breakdown_point, :delete_trigger,
                        :sales_mistakes, :warning_signals, :recoverable,
                        :customer_psychology_path, :churn_pattern,
                        :avoidable_experience, :recommended_agent_rules,
                        :raw
                    )
                    ON DUPLICATE KEY UPDATE
                        session_id = VALUES(session_id),
                        delete_stage_summary = VALUES(delete_stage_summary),
                        customer_profile = VALUES(customer_profile),
                        initial_need = VALUES(initial_need),
                        unmet_need = VALUES(unmet_need),
                        trust_breakdown_point = VALUES(trust_breakdown_point),
                        delete_trigger = VALUES(delete_trigger),
                        sales_mistakes = VALUES(sales_mistakes),
                        warning_signals = VALUES(warning_signals),
                        recoverable = VALUES(recoverable),
                        customer_psychology_path = VALUES(customer_psychology_path),
                        churn_pattern = VALUES(churn_pattern),
                        avoidable_experience = VALUES(avoidable_experience),
                        recommended_agent_rules = VALUES(recommended_agent_rules),
                        raw_result = VALUES(raw_result),
                        analyzed_at = NOW()
                """),
                {
                    "cid": customer_id,
                    "sid": session_id,
                    **fields,
                    "raw": json.dumps(raw, ensure_ascii=False, default=str),
                },
            )

    with_db_retry(_run)


def list_delete_analyses(limit: int = 200, since_date: datetime | None = None) -> list[dict]:
    init_db()
    engine = get_engine()
    sql = """
        SELECT da.*,
               c.contact_name,
               c.original_session_id,
               c.sales_name,
               c.status AS customer_status
        FROM delete_analysis da
        JOIN customers c ON c.id = da.customer_id
        WHERE 1=1
    """
    params: dict[str, Any] = {"lim": int(limit)}
    if since_date is not None:
        sql += " AND da.analyzed_at >= :since"
        params["since"] = since_date
    sql += " ORDER BY da.analyzed_at DESC LIMIT :lim"
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_delete_analyses_since_days(days: int = 7, limit: int = 500) -> list[dict]:
    from datetime import timedelta

    since = datetime.now() - timedelta(days=max(1, int(days)))
    return list_delete_analyses(limit=limit, since_date=since)


def list_distinct_delete_agent_rules() -> list[str]:
    init_db()
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT recommended_agent_rules
                FROM delete_analysis
                WHERE recommended_agent_rules IS NOT NULL
                  AND TRIM(recommended_agent_rules) != ''
            """),
        ).fetchall()
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        rule = str(row[0] or "").strip()
        if rule and rule not in seen:
            seen.add(rule)
            out.append(rule)
    return out


def save_daily_delete_learning_run(
    run_date,
    new_delete_customers: int,
    analyzed_customers: int,
    failed_customers: int,
    summary: str,
) -> None:
    init_db()

    def _run() -> None:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO daily_delete_learning_runs (
                        run_date, new_delete_customers, analyzed_customers,
                        failed_customers, summary
                    ) VALUES (
                        :dt, :new_del, :analyzed, :failed, :summary
                    )
                    ON DUPLICATE KEY UPDATE
                        new_delete_customers = VALUES(new_delete_customers),
                        analyzed_customers = VALUES(analyzed_customers),
                        failed_customers = VALUES(failed_customers),
                        summary = VALUES(summary)
                """),
                {
                    "dt": run_date,
                    "new_del": new_delete_customers,
                    "analyzed": analyzed_customers,
                    "failed": failed_customers,
                    "summary": summary,
                },
            )

    with_db_retry(_run)
