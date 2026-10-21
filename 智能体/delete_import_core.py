# -*- coding: utf-8 -*-
"""已删除客户聊天导入 · 网页版 / 定时任务共用核心逻辑。"""

from __future__ import annotations

from typing import Any

import qc_core as core
from sqlalchemy import text

from db import get_engine, init_db, normalize_session_id, upsert_sessions, format_database_error
from deal_import_core import (
    _dedupe_sessions,
    empty_summary,
    finalize_import_summary,
    get_mysql_target_label,
    load_import_config,
)


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


def empty_delete_summary() -> dict:
    summary = empty_summary()
    summary["marked_deleted"] = 0
    return summary


def mark_sessions_as_deleted_from_list(sessions: list[dict]) -> int:
    return mark_sessions_as_deleted(sessions)


def _filter_delete_sessions(sessions: list[dict]) -> tuple[list[dict], list[dict]]:
    kept, skipped = [], []
    for session in sessions:
        if core.count_valid_customer_segments(session.get("对话") or "") > 6:
            kept.append(session)
        else:
            skipped.append({
                "session_id": session.get("会话ID", ""), "contact_name": session.get("联系人", ""),
                "status": "跳过", "reason": "客户有效发言不足 7 条",
            })
    return kept, skipped


def import_delete_from_file_dfs(
    file_dfs: list[tuple[str, Any]],
    column_map: dict | None = None,
) -> tuple[list[dict], dict]:
    all_sessions: list[dict] = []
    summary = empty_delete_summary()

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

        part, skipped = _filter_delete_sessions(part)
        summary["session_details"].extend(skipped)
        if not part:
            file_stat["status"] = "跳过"
            file_stat["reason"] = "客户有效发言不足 7 条"
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
        summary["file_stats"].append(file_stat)
        all_sessions.extend(part)

    sessions = _dedupe_sessions(all_sessions)
    summary["session_count"] = len(sessions)
    summary["file_count"] = len(file_dfs)
    marked = mark_sessions_as_deleted(sessions) if sessions else 0
    summary["marked_deleted"] = marked
    return sessions, finalize_import_summary(summary)


def run_delete_learning(
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

    from delete_intelligence import analyze_unlearned_deletes

    return analyze_unlearned_deletes(cfg, limit=limit, on_progress=on_progress)


def execute_delete_import(
    *,
    file_dfs: list[tuple[str, Any]] | None = None,
    column_map: dict | None = None,
    dry_run: bool = False,
    run_analyze: bool = False,
    analyze_limit: int = 20,
    qc_cfg: dict | None = None,
) -> dict:
    import_cfg = load_import_config()
    target = get_mysql_target_label(import_cfg)
    out = {
        "ok": False,
        "target": target,
        "sessions": [],
        "summary": empty_delete_summary(),
        "learn_result": None,
        "error": "",
    }

    if not file_dfs:
        out["error"] = "未提供文件"
        return out

    sessions, diag, _ = core.load_sessions(
        file_dfs=file_dfs,
        column_map=column_map,
        time_scope=core.TIME_SCOPE_ALL,
    )
    for d in diag:
        if d.get("error"):
            out["summary"]["errors"].append(f"{d.get('file')}: {d['error']}")

    sessions, skipped = _filter_delete_sessions(sessions)
    out["sessions"] = sessions
    out["summary"]["session_count"] = len(sessions)
    out["summary"]["session_details"].extend(skipped)

    if not sessions:
        if skipped:
            out["ok"] = True
            out["summary"] = finalize_import_summary(out["summary"])
        else:
            out["error"] = "没有可导入的会话，请检查 Excel 是否含「会话ID」和「消息内容」列。"
        return out

    if dry_run:
        out["ok"] = True
        return out

    try:
        _, summary = import_delete_from_file_dfs(file_dfs, column_map=column_map)
        out["summary"] = summary
    except Exception as e:
        out["error"] = format_database_error(e)
        return out

    if run_analyze:
        try:
            learn = run_delete_learning(analyze_limit, qc_cfg=qc_cfg, on_progress=None)
            out["learn_result"] = learn
            out["summary"]["learn_total"] = learn.get("total", 0)
            out["summary"]["learn_success"] = learn.get("success", 0)
            out["summary"]["learn_failed"] = learn.get("failed", 0)
        except Exception as e:
            out["error"] = f"导入成功，但流失复盘失败：{e}"
            out["ok"] = True
            return out

    out["ok"] = True
    return out
