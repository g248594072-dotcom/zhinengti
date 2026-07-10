# -*- coding: utf-8 -*-
"""已成交客户聊天导入 · 网页版 / 桌面脚本共用核心逻辑。"""

from __future__ import annotations

import json
import os
from typing import Any

import qc_core as core
from db import init_db, get_engine, normalize_session_id, upsert_sessions
from sqlalchemy import text

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_APP_DIR, "import_deal_config.json")
_CONFIG_EXAMPLE = os.path.join(_APP_DIR, "import_deal_config.example.json")


def load_import_config() -> dict:
    if not os.path.isfile(_CONFIG_PATH):
        if os.path.isfile(_CONFIG_EXAMPLE):
            with open(_CONFIG_EXAMPLE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            save_import_config(cfg)
        else:
            cfg = {
                "mysql_database": "customer_ai",
                "default_analyze_limit": 20,
                "import_last_dir": "",
                "salesmartly": {
                    "deal_tag_keywords": ["全款", "定金", "分期"],
                    "date_label_category": "澳大利亚",
                    "date_label_name": "日期",
                },
            }
            save_import_config(cfg)
        return cfg
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_import_config(cfg: dict) -> None:
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def get_mysql_target_label(cfg: dict | None = None) -> str:
    cfg = cfg or load_import_config()
    db_name = (cfg.get("mysql_database") or "").strip()
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_APP_DIR, ".env"))
        host = os.getenv("MYSQL_HOST", "127.0.0.1")
        port = os.getenv("MYSQL_PORT", "3306")
        env_db = os.getenv("MYSQL_DATABASE", "")
        if env_db and db_name and env_db != db_name:
            return f"{host}:{port}/{env_db}（.env）⚠ 与配置库名 {db_name} 不一致"
        return f"{host}:{port}/{env_db or db_name or 'customer_ai'}"
    except Exception:
        return db_name or "customer_ai"


def empty_summary() -> dict:
    return {
        "customers_created": 0,
        "customers_updated": 0,
        "sessions_created": 0,
        "sessions_updated": 0,
        "messages_inserted": 0,
        "conflicts": 0,
        "unchanged_sessions": 0,
        "session_count": 0,
        "file_count": 0,
        "marked_deal": 0,
        "errors": [],
        "session_details": [],
        "file_stats": [],
        "settlement": {},
    }


def finalize_import_summary(summary: dict) -> dict:
    """汇总导入结算：成功/跳过/失败数量及原因分组。"""
    details = summary.get("session_details") or []
    success_items = [d for d in details if d.get("status") == "成功"]
    skipped_items = [d for d in details if d.get("status") == "跳过"]
    failed_items = [d for d in details if d.get("status") == "失败"]

    skip_by_reason: dict[str, int] = {}
    for d in skipped_items:
        reason = (d.get("reason") or "未知原因").strip()
        skip_by_reason[reason] = skip_by_reason.get(reason, 0) + 1

    fail_by_reason: dict[str, int] = {}
    for d in failed_items:
        reason = (d.get("reason") or "未知原因").strip()
        fail_by_reason[reason] = fail_by_reason.get(reason, 0) + 1

    summary["settlement"] = {
        "total_sessions": len(details),
        "success_count": len(success_items),
        "skipped_count": len(skipped_items),
        "failed_count": len(failed_items),
        "skip_by_reason": skip_by_reason,
        "fail_by_reason": fail_by_reason,
        "success_items": success_items,
        "skipped_items": skipped_items,
        "failed_items": failed_items,
    }
    return summary


def _dedupe_sessions(sessions: list[dict]) -> list[dict]:
    uniq: dict[str, dict] = {}
    for s in sessions:
        sid = s.get("会话ID", "")
        if sid not in uniq or len(s.get("对话") or "") > len(uniq[sid].get("对话") or ""):
            uniq[sid] = s
    return list(uniq.values())


def mark_sessions_as_deal(sessions: list[dict]) -> int:
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
                    SET c.status = '已成交',
                        c.deal_at = COALESCE(c.deal_at, NOW()),
                        c.updated_at = NOW()
                    WHERE s.original_session_id = :sid
                """),
                {"sid": sid},
            )
            marked += result.rowcount or 0
    return marked


def import_from_file_dfs(
    file_dfs: list[tuple[str, Any]],
    column_map: dict | None = None,
) -> tuple[list[dict], dict]:
    """从 (文件名, DataFrame) 列表导入并标记已成交。"""
    init_db()
    all_sessions: list[dict] = []
    summary = empty_summary()

    for name, df in file_dfs:
        part, diag, _ = core.load_sessions(
            file_dfs=[(name, df)],
            column_map=column_map,
            time_scope=core.TIME_SCOPE_ALL,
        )
        file_stat = {
            "file_name": name,
            "rows": len(df),
            "sessions_parsed": len(part),
            "status": "成功" if part else "失败",
            "reason": "",
        }
        for d in diag:
            if d.get("error"):
                summary["errors"].append(f"{d.get('file')}: {d['error']}")
                file_stat["reason"] = d.get("error", "")
        if not part:
            msg = f"{name}: 无有效会话（请检查是否含会话ID与消息内容）"
            summary["errors"].append(msg)
            file_stat["status"] = "失败"
            file_stat["reason"] = file_stat["reason"] or "无有效会话"
            summary["file_stats"].append(file_stat)
            continue

        r = upsert_sessions(part, source_file=name)
        for k in ("customers_created", "customers_updated", "sessions_created",
                  "sessions_updated", "messages_inserted", "conflicts",
                  "unchanged_sessions"):
            summary[k] += r.get(k, 0)
        summary["errors"].extend(r.get("errors") or [])
        summary["session_details"].extend(r.get("session_details") or [])
        file_stat["messages_inserted"] = r.get("messages_inserted", 0)
        file_stat["sessions_ok"] = sum(
            1 for x in (r.get("session_details") or []) if x.get("status") == "成功"
        )
        file_stat["sessions_skipped"] = sum(
            1 for x in (r.get("session_details") or []) if x.get("status") == "跳过"
        )
        file_stat["sessions_failed"] = sum(
            1 for x in (r.get("session_details") or []) if x.get("status") == "失败"
        )
        summary["file_stats"].append(file_stat)
        all_sessions.extend(part)

    sessions = _dedupe_sessions(all_sessions)
    summary["session_count"] = len(sessions)
    summary["file_count"] = len(file_dfs)
    marked = mark_sessions_as_deal(sessions)
    summary["marked_deal"] = marked
    return sessions, finalize_import_summary(summary)


def import_from_paths(files: list[str]) -> tuple[list[dict], dict]:
    """从磁盘 xlsx 路径导入（桌面脚本用）。"""
    init_db()
    all_sessions: list[dict] = []
    summary = empty_summary()

    for f in files:
        part, diag, _ = core.load_sessions(files=[f])
        for d in diag:
            if d.get("error"):
                summary["errors"].append(f"{d.get('file')}: {d['error']}")
        if not part:
            summary["errors"].append(f"{os.path.basename(f)}: 无有效会话")
            continue

        r = upsert_sessions(part, source_file=os.path.basename(f))
        for k in ("customers_created", "customers_updated", "sessions_created",
                  "sessions_updated", "messages_inserted", "conflicts",
                  "unchanged_sessions"):
            summary[k] += r.get(k, 0)
        summary["errors"].extend(r.get("errors") or [])
        summary["session_details"].extend(r.get("session_details") or [])
        all_sessions.extend(part)

    sessions = _dedupe_sessions(all_sessions)
    summary["session_count"] = len(sessions)
    summary["file_count"] = len(files)
    marked = mark_sessions_as_deal(sessions)
    summary["marked_deal"] = marked
    return sessions, finalize_import_summary(summary)


def run_deal_learning(
    limit: int,
    qc_cfg: dict | None = None,
    on_progress=None,
) -> dict:
    if qc_cfg is None:
        cfg, warn = core.load_config_from_disk()
        if warn:
            raise RuntimeError(f"配置错误：{warn}")
    else:
        cfg = qc_cfg
    if not cfg.get("api_key") or str(cfg["api_key"]).strip() in ("", "YOUR_API_KEY_HERE"):
        raise RuntimeError("未配置 API Key，请在 qc_config.json 中填写 api_key")

    from deal_intelligence import analyze_unlearned_deals

    return analyze_unlearned_deals(cfg, limit=limit, on_progress=on_progress)


def execute_deal_import(
    *,
    file_dfs: list[tuple[str, Any]] | None = None,
    files: list[str] | None = None,
    column_map: dict | None = None,
    dry_run: bool = False,
    run_analyze: bool = False,
    analyze_limit: int = 20,
    qc_cfg: dict | None = None,
) -> dict:
    """
    统一导入入口。返回：
    {ok, target, sessions, summary, learn_result, error}
    """
    import_cfg = load_import_config()
    target = get_mysql_target_label(import_cfg)
    out = {
        "ok": False,
        "target": target,
        "sessions": [],
        "summary": empty_summary(),
        "learn_result": None,
        "error": "",
    }

    if file_dfs:
        sessions, diag, _ = core.load_sessions(
            file_dfs=file_dfs,
            column_map=column_map,
            time_scope=core.TIME_SCOPE_ALL,
        )
        for d in diag:
            if d.get("error"):
                out["summary"]["errors"].append(f"{d.get('file')}: {d['error']}")
    elif files:
        sessions, diag, _ = core.load_sessions(files=files)
        for d in diag:
            if d.get("error"):
                out["summary"]["errors"].append(f"{d.get('file')}: {d['error']}")
    else:
        out["error"] = "未提供文件"
        return out

    out["sessions"] = sessions
    out["summary"]["session_count"] = len(sessions)

    if not sessions:
        out["error"] = "没有可导入的会话，请检查 Excel 是否含「会话ID」和「消息内容」列。"
        return out

    if dry_run:
        out["ok"] = True
        return out

    try:
        if file_dfs:
            _, summary = import_from_file_dfs(file_dfs, column_map=column_map)
        else:
            _, summary = import_from_paths(files)
        out["summary"] = summary
    except Exception as e:
        out["error"] = str(e)
        return out

    if run_analyze:
        try:
            learn = run_deal_learning(analyze_limit, qc_cfg=qc_cfg, on_progress=None)
            out["learn_result"] = learn
            out["summary"]["learn_total"] = learn.get("total", 0)
            out["summary"]["learn_success"] = learn.get("success", 0)
            out["summary"]["learn_failed"] = learn.get("failed", 0)
        except Exception as e:
            out["error"] = f"导入成功，但心理学习失败：{e}"
            out["ok"] = True
            return out

    out["ok"] = True
    return out
