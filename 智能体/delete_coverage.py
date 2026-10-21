# -*- coding: utf-8 -*-
"""删除学习 · 阶段一覆盖判断。"""

from __future__ import annotations

import logging

import qc_core as core
from delete_context import build_compact_delete_refs, build_deal_comparison_note, extract_delete_key_window

logger = logging.getLogger(__name__)

_CN_FROM_DB = {
    "delete_stage_summary": "删除阶段总结",
    "customer_profile": "客户画像",
    "initial_need": "初始需求",
    "unmet_need": "未满足的需求",
    "trust_breakdown_point": "信任崩塌点",
    "delete_trigger": "删除触发点",
    "sales_mistakes": "销售踩雷动作",
    "warning_signals": "删除前预警信号",
    "recoverable": "是否可挽回",
    "customer_psychology_path": "客户心理路径",
    "churn_pattern": "流失模式分类",
    "avoidable_experience": "可规避销售经验",
    "recommended_agent_rules": "智能体判断规则",
}


def _clip(text: str, n: int = 240) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else t[: n - 3] + "..."


def load_delete_supplement_excerpt(max_chars: int = 1200) -> str:
    import re

    try:
        raw = core.load_prompt("delete_learned_supplement.md")
    except FileNotFoundError:
        return ""
    raw = re.sub(r"<!--[\s\S]*?-->", "", raw or "").strip()
    if len(raw) <= max_chars:
        return raw
    return raw[: max_chars - 3] + "..."


def delete_record_has_rules(record: dict) -> bool:
    return bool(str(record.get("recommended_agent_rules") or "").strip())


def inherited_analysis_from_ref(best: dict, reason: str) -> dict:
    out: dict = {}
    for db_col, cn_key in _CN_FROM_DB.items():
        val = best.get(db_col) or best.get(cn_key) or ""
        out[cn_key] = str(val).strip()
        out[db_col] = out[cn_key]
    ref_name = best.get("contact_name") or best.get("original_session_id") or "参考案例"
    prefix = f"【继承自 {ref_name}】{reason}"
    out["删除阶段总结"] = prefix + "\n" + (out.get("删除阶段总结") or "")
    out["delete_stage_summary"] = out["删除阶段总结"]
    out["_学习模式"] = "继承跳过"
    out["_参考案例"] = ref_name
    out["_匹配度"] = best.get("_match_score")
    return out


def assess_delete_learning_need(dialog: str, cfg: dict) -> dict:
    from delete_context import find_similar_deletes

    incremental_min = float(cfg.get("delete_learn_incremental_score", 3.0))
    incremental_enabled = cfg.get("delete_learn_incremental_enabled", True)
    ref_limit = int(cfg.get("delete_learn_ref_limit", 3))
    deal_compare_limit = int(cfg.get("delete_learn_deal_compare_limit", 2))

    supplement = load_delete_supplement_excerpt()
    similar = find_similar_deletes(
        dialog,
        limit=int(cfg.get("delete_context_limit") or 5),
        prefer_today=cfg.get("delete_context_prefer_today", True),
    )
    best = similar[0] if similar else None
    best_score = float(best.get("_match_score", 0)) if best else 0.0
    deal_comparison = build_deal_comparison_note(dialog, limit=deal_compare_limit)
    key_window = extract_delete_key_window(dialog)

    base = {
        "similar_deletes": similar,
        "best_match": best,
        "best_score": best_score,
        "supplement_excerpt": supplement,
        "ref_compact": build_compact_delete_refs(similar, limit=ref_limit),
        "deal_comparison": deal_comparison,
        "delete_key_window": key_window,
    }

    # 相似案例只提供参考；任何客户都必须依据自己的聊天实际调用 AI。
    if incremental_enabled and similar and best_score >= incremental_min:
        action = "incremental"
        reason = f"参考相似案例（匹配度 {best_score}），独立分析当前客户聊天"
    else:
        action = "full"
        reason = "依据当前客户聊天执行全量复盘"

    base.update({"action": action, "reason": reason})
    return base


def _llm_coverage_check(
    dialog: str,
    similar: list[dict],
    supplement: str,
    cfg: dict,
) -> tuple[bool, str]:
    try:
        template = core.load_prompt("delete_learning_coverage.md")
    except FileNotFoundError:
        return False, ""

    from deal_context import infer_dialog_signals

    signals = infer_dialog_signals(dialog)
    snippet = signals.get("customer_snippet") or _clip(dialog, 2000)
    refs = build_compact_delete_refs(similar, limit=3)
    user = (
        template.replace("{{references}}", refs)
        .replace("{{supplement}}", supplement or "（暂无）")
        .replace("{{dialog_snippet}}", snippet)
    )
    system = "你是知识库管理员。只输出 JSON。"
    result = core.call_llm_prompt(cfg, system, user, max_chars=4000)
    if isinstance(result, dict) and result.get("_错误"):
        logger.warning("delete coverage LLM failed: %s", result["_错误"])
        return False, ""
    fit = result.get("符合")
    if isinstance(fit, str):
        fit = fit.strip().lower() in ("true", "是", "yes", "1")
    reason = str(result.get("原因") or result.get("主要差异点") or "").strip()
    return bool(fit), reason
