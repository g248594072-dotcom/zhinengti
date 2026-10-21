# -*- coding: utf-8 -*-
"""删除客户流失经验检索 · 质检 prompt 注入 · 成交对比案例。"""

from __future__ import annotations

import logging
import re

from deal_context import (
    CONCERN_KEYWORDS,
    STAGE_KEYWORDS,
    _clip,
    _is_today,
    _token_overlap,
    infer_dialog_signals,
)

logger = logging.getLogger(__name__)


def extract_delete_key_window(dialog: str, max_rounds: int = 20) -> str:
    """截取删除前最后若干轮客户相关互动。"""
    if not (dialog or "").strip():
        return "（无）"
    lines = [ln for ln in dialog.splitlines() if ln.strip()]
    if not lines:
        return "（无）"

    cust_indices = [
        i for i, ln in enumerate(lines)
        if re.search(r"客户\s*[:：]", ln) or re.search(r"customer\s*[:：]", ln, re.I)
    ]
    if cust_indices:
        start_idx = cust_indices[max(0, len(cust_indices) - max_rounds)]
        start = max(0, start_idx - 3)
        return "\n".join(lines[start:])
    return "\n".join(lines[-max(30, max_rounds * 2):])


def score_delete_relevance(dialog: str, signals: dict, record: dict) -> float:
    score = 0.0
    concern = signals.get("concern") or "无"
    unmet = str(record.get("unmet_need") or "")
    profile = str(record.get("customer_profile") or "")
    summary = str(record.get("delete_stage_summary") or "")
    psych = str(record.get("customer_psychology_path") or "")
    churn = str(record.get("churn_pattern") or "")
    blob = f"{unmet} {profile} {summary} {psych} {churn}"

    if concern != "无" and concern in blob:
        score += 4.0
    for c in signals.get("concerns") or []:
        if c in blob:
            score += 1.5

    stage = signals.get("stage") or ""
    if stage and stage in summary:
        score += 2.0
    for s in signals.get("stages") or []:
        if s in summary:
            score += 0.8

    snippet = signals.get("customer_snippet") or dialog
    score += min(_token_overlap(snippet, profile) * 0.6, 3.0)
    score += min(_token_overlap(dialog[-2000:], psych) * 0.4, 2.0)

    analyzed_at = record.get("analyzed_at")
    if _is_today(analyzed_at):
        score += 2.0

    if str(record.get("recommended_agent_rules") or "").strip():
        score += 0.5
    if str(record.get("avoidable_experience") or "").strip():
        score += 0.5

    return score


def _load_all_deletes() -> list[dict]:
    try:
        from db import list_delete_analyses

        import json
        records = list_delete_analyses(limit=300)
        def independent(record):
            raw = record.get("raw_result") or {}
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (ValueError, TypeError):
                    return False
            return isinstance(raw, dict) and raw.get("_学习模式") != "继承跳过"
        return [r for r in records if independent(r)]
    except Exception as e:
        logger.warning("加载删除分析失败: %s", e)
        return []


def find_similar_deletes(
    dialog: str,
    limit: int = 5,
    prefer_today: bool = True,
) -> list[dict]:
    records = _load_all_deletes()
    if not records:
        return []

    signals = infer_dialog_signals(dialog)
    scored: list[tuple[float, dict]] = []
    for rec in records:
        s = score_delete_relevance(dialog, signals, rec)
        if s > 0:
            scored.append((s, rec))

    if not scored:
        fallback = records[:limit]
        for d in fallback:
            d["_match_score"] = 0.0
            d["_match_reason"] = "最近删除（无强匹配）"
        return fallback

    scored.sort(key=lambda x: (-x[0], str(x[1].get("analyzed_at") or "")))
    if prefer_today:
        today_hits = [(s, d) for s, d in scored if _is_today(d.get("analyzed_at"))]
        if today_hits:
            scored = today_hits + [(s, d) for s, d in scored if not _is_today(d.get("analyzed_at"))]

    out = []
    for s, rec in scored[: max(1, int(limit))]:
        item = dict(rec)
        item["_match_score"] = round(s, 2)
        item["_match_reason"] = _build_match_reason(signals, rec)
        out.append(item)
    return out


def _build_match_reason(signals: dict, record: dict) -> str:
    parts = []
    concern = signals.get("concern")
    blob = f"{record.get('unmet_need')} {record.get('churn_pattern')}"
    if concern and concern in blob:
        parts.append(f"顾虑≈{concern}")
    stage = signals.get("stage")
    if stage and stage in str(record.get("delete_stage_summary") or ""):
        parts.append(f"阶段≈{stage}")
    churn = str(record.get("churn_pattern") or "").strip()
    if churn:
        parts.append(f"流失≈{churn[:12]}")
    if not parts:
        parts.append("话术/画像相近")
    return "、".join(parts)


def build_compact_delete_refs(records: list[dict], limit: int = 3) -> str:
    if not records:
        return "（无已有参考案例）"
    lines = []
    for i, rec in enumerate(records[: max(1, int(limit))], 1):
        name = rec.get("contact_name") or rec.get("original_session_id") or f"案例{i}"
        reason = rec.get("_match_reason") or ""
        score = rec.get("_match_score")
        header = f"{i}. {name}"
        if reason:
            header += f"（{reason}"
            if score is not None:
                header += f"，匹配{score}"
            header += "）"
        lines.append(header)
        if rec.get("churn_pattern"):
            lines.append(f"   流失模式：{_clip(str(rec['churn_pattern']), 80)}")
        if rec.get("sales_mistakes"):
            lines.append(f"   踩雷：{_clip(str(rec['sales_mistakes']), 160)}")
        if rec.get("recommended_agent_rules"):
            lines.append(f"   规则：{_clip(str(rec['recommended_agent_rules']), 280)}")
    return "\n".join(lines)


def build_deal_comparison_note(dialog: str, limit: int = 2) -> str:
    """检索同顾虑但成交的案例，供删除复盘对照。"""
    from deal_context import find_similar_deals

    deals = find_similar_deals(dialog, limit=limit, prefer_today=False)
    if not deals:
        return "（无同顾虑成交对比案例）"

    lines = ["以下客户有相似顾虑/阶段但最终成交，请对照差异分析删除原因：", ""]
    for i, deal in enumerate(deals, 1):
        name = deal.get("contact_name") or deal.get("original_session_id") or f"案例{i}"
        reason = deal.get("_match_reason") or ""
        lines.append(f"{i}. {name}" + (f"（{reason}）" if reason else ""))
        if deal.get("main_objection"):
            lines.append(f"   主要顾虑：{_clip(str(deal['main_objection']), 100)}")
        if deal.get("trust_trigger"):
            lines.append(f"   信任触发：{_clip(str(deal['trust_trigger']), 120)}")
        if deal.get("sales_key_actions"):
            lines.append(f"   关键动作：{_clip(str(deal['sales_key_actions']), 160)}")
        if deal.get("reusable_sales_experience"):
            lines.append(f"   成交经验：{_clip(str(deal['reusable_sales_experience']), 200)}")
    return "\n".join(lines)


def build_delete_reference_note(records: list[dict]) -> str:
    if not records:
        return ""

    lines = [
        "【参考删除案例 · 仅供对照判断，以下均为已删除客户的流失复盘】",
        "请结合当前会话证据使用：相似处可借鉴预警信号与踩雷点，不可生搬硬套。",
        "",
    ]
    for i, rec in enumerate(records, 1):
        name = rec.get("contact_name") or rec.get("original_session_id") or f"案例{i}"
        reason = rec.get("_match_reason") or ""
        score = rec.get("_match_score")
        header = f"案例{i}：{name}"
        if reason:
            header += f"（{reason}"
            if score is not None:
                header += f"，匹配度{score}"
            header += "）"
        lines.append(header)
        if rec.get("churn_pattern"):
            lines.append(f"· 流失模式：{_clip(str(rec['churn_pattern']), 100)}")
        if rec.get("warning_signals"):
            lines.append(f"· 预警信号：{_clip(str(rec['warning_signals']), 160)}")
        if rec.get("sales_mistakes"):
            lines.append(f"· 销售踩雷：{_clip(str(rec['sales_mistakes']), 200)}")
        if rec.get("avoidable_experience"):
            lines.append(f"· 可规避经验：{_clip(str(rec['avoidable_experience']), 240)}")
        if rec.get("recommended_agent_rules"):
            lines.append(f"· 智能体判断规则：{_clip(str(rec['recommended_agent_rules']), 320)}")
        lines.append("")
    return "\n".join(lines).strip()


def get_delete_context_for_qc(dialog: str, cfg: dict | None = None) -> tuple[str, list[dict]]:
    cfg = cfg or {}
    if cfg.get("delete_context_enabled") is False:
        return "", []

    limit = int(cfg.get("delete_context_limit") or 3)
    prefer_today = cfg.get("delete_context_prefer_today", True)
    records = find_similar_deletes(dialog, limit=limit, prefer_today=prefer_today)
    if not records:
        return "", []
    return build_delete_reference_note(records), records


def summarize_delete_refs(records: list[dict]) -> str:
    if not records:
        return ""
    names = [
        str(d.get("contact_name") or d.get("original_session_id") or "?")
        for d in records[:5]
    ]
    return f"{len(records)}条：" + "、".join(names)
