# -*- coding: utf-8 -*-
"""成交学习 · 阶段一覆盖判断 · 参考案例检索（供学习决策，非质检注入）。"""

from __future__ import annotations

import logging
import re

import qc_core as core

logger = logging.getLogger(__name__)

_CN_FROM_DB = {
    "deal_stage_summary": "成交阶段总结",
    "customer_profile": "客户画像",
    "core_need": "核心需求",
    "core_pain_point": "核心痛点",
    "main_objection": "主要顾虑",
    "trust_trigger": "信任触发点",
    "deal_trigger": "成交触发点",
    "key_turning_points": "关键转折点",
    "sales_key_actions": "销售关键动作",
    "customer_psychology_path": "客户心理路径",
    "deal_signals": "成交信号",
    "reusable_sales_experience": "可复用销售经验",
    "recommended_agent_rules": "智能体判断规则",
}


def _clip(text: str, n: int = 240) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else t[: n - 3] + "..."


def load_supplement_excerpt(max_chars: int = 1200) -> str:
    try:
        raw = core.load_prompt("deal_learned_supplement.md")
    except FileNotFoundError:
        return ""
    raw = re.sub(r"<!--[\s\S]*?-->", "", raw or "").strip()
    if len(raw) <= max_chars:
        return raw
    return raw[: max_chars - 3] + "..."


def build_compact_deal_refs(deals: list[dict], limit: int = 3) -> str:
    """精简参考案例，供学习阶段 prompt 使用。"""
    if not deals:
        return "（无已有参考案例）"
    lines = []
    for i, deal in enumerate(deals[: max(1, int(limit))], 1):
        name = deal.get("contact_name") or deal.get("original_session_id") or f"案例{i}"
        reason = deal.get("_match_reason") or ""
        score = deal.get("_match_score")
        header = f"{i}. {name}"
        if reason:
            header += f"（{reason}"
            if score is not None:
                header += f"，匹配{score}"
            header += "）"
        lines.append(header)
        if deal.get("main_objection"):
            lines.append(f"   顾虑：{_clip(str(deal['main_objection']), 100)}")
        if deal.get("recommended_agent_rules"):
            lines.append(f"   规则：{_clip(str(deal['recommended_agent_rules']), 280)}")
        if deal.get("reusable_sales_experience"):
            lines.append(f"   经验：{_clip(str(deal['reusable_sales_experience']), 160)}")
    return "\n".join(lines)


def deal_record_has_rules(deal: dict) -> bool:
    return bool(str(deal.get("recommended_agent_rules") or "").strip())


def inherited_analysis_from_ref(best: dict, reason: str) -> dict:
    """从参考案例生成可入库的分析结果（继承跳过）。"""
    out: dict = {}
    for db_col, cn_key in _CN_FROM_DB.items():
        val = best.get(db_col) or best.get(cn_key) or ""
        out[cn_key] = str(val).strip()
        out[db_col] = out[cn_key]
    ref_name = best.get("contact_name") or best.get("original_session_id") or "参考案例"
    prefix = f"【继承自 {ref_name}】{reason}"
    out["成交阶段总结"] = prefix + "\n" + (out.get("成交阶段总结") or "")
    out["deal_stage_summary"] = out["成交阶段总结"]
    out["_学习模式"] = "继承跳过"
    out["_参考案例"] = ref_name
    out["_匹配度"] = best.get("_match_score")
    return out


def assess_deal_learning_need(dialog: str, cfg: dict) -> dict:
    """阶段一：检索已有分析并判断 skip / incremental / full。"""
    from deal_context import find_similar_deals

    skip_score = float(cfg.get("deal_learn_skip_score", 6.0))
    incremental_min = float(cfg.get("deal_learn_incremental_score", 3.0))
    incremental_enabled = cfg.get("deal_learn_incremental_enabled", True)
    ref_limit = int(cfg.get("deal_learn_ref_limit", 3))
    use_llm = bool(cfg.get("deal_learn_coverage_llm", False))

    supplement = load_supplement_excerpt()
    similar = find_similar_deals(
        dialog,
        limit=int(cfg.get("deal_context_limit") or 5),
        prefer_today=cfg.get("deal_context_prefer_today", True),
    )
    best = similar[0] if similar else None
    best_score = float(best.get("_match_score", 0)) if best else 0.0

    base = {
        "similar_deals": similar,
        "best_match": best,
        "best_score": best_score,
        "supplement_excerpt": supplement,
        "ref_compact": build_compact_deal_refs(similar, limit=ref_limit),
    }

    if best and best_score >= skip_score and deal_record_has_rules(best):
        action = "skip"
        reason = (
            f"与「{best.get('contact_name') or best.get('original_session_id')}」"
            f"高度相似（匹配度 {best_score}），已有分析可覆盖"
        )
    elif (
        use_llm
        and best
        and best_score >= max(skip_score - 2.0, incremental_min)
        and deal_record_has_rules(best)
    ):
        llm_ok, llm_reason = _llm_coverage_check(dialog, similar, supplement, cfg)
        if llm_ok:
            action = "skip"
            reason = llm_reason or "知识库已覆盖该类型客户"
        elif incremental_enabled and similar and best_score >= incremental_min:
            action = "incremental"
            reason = llm_reason or "与已有案例有差异，增量更新"
        else:
            action = "full"
            reason = llm_reason or "无足够参考，全量复盘"
    elif incremental_enabled and similar and best_score >= incremental_min:
        action = "incremental"
        reason = f"与已有案例部分相似（匹配度 {best_score}），增量对比后更新知识"
    else:
        action = "full"
        reason = "无足够相似的已有分析，执行全量复盘"

    base.update({"action": action, "reason": reason})
    return base


def _llm_coverage_check(
    dialog: str,
    similar: list[dict],
    supplement: str,
    cfg: dict,
) -> tuple[bool, str]:
    try:
        template = core.load_prompt("deal_learning_coverage.md")
    except FileNotFoundError:
        return False, ""

    from deal_context import infer_dialog_signals

    signals = infer_dialog_signals(dialog)
    snippet = signals.get("customer_snippet") or _clip(dialog, 2000)
    refs = build_compact_deal_refs(similar, limit=3)
    user = (
        template.replace("{{references}}", refs)
        .replace("{{supplement}}", supplement or "（暂无）")
        .replace("{{dialog_snippet}}", snippet)
    )
    system = "你是知识库管理员。只输出 JSON。"
    result = core.call_llm_prompt(cfg, system, user, max_chars=4000)
    if isinstance(result, dict) and result.get("_错误"):
        logger.warning("coverage LLM failed: %s", result["_错误"])
        return False, ""
    fit = result.get("符合")
    if isinstance(fit, str):
        fit = fit.strip().lower() in ("true", "是", "yes", "1")
    reason = str(result.get("原因") or result.get("主要差异点") or "").strip()
    return bool(fit), reason
