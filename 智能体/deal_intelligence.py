# -*- coding: utf-8 -*-
"""成交客户心理分析 · 每日学习任务（两阶段：覆盖判断 → 增量/全量学习）。"""

from __future__ import annotations

import logging
from datetime import date

import qc_core as core
from db import (
    get_customer_full_dialog,
    get_unanalyzed_deal_customers,
    init_db,
    save_deal_analysis,
    save_daily_learning_run,
)
from deal_coverage import (
    assess_deal_learning_need,
    inherited_analysis_from_ref,
)

logger = logging.getLogger(__name__)

DEAL_PROMPT_FILE = "deal_learning.md"
INCREMENTAL_PROMPT_FILE = "deal_learning_incremental.md"


def load_deal_prompt() -> str:
    return core.load_prompt(DEAL_PROMPT_FILE)


def _build_deal_user_prompt(dialog: str) -> str:
    template = load_deal_prompt()
    if "{{dialog}}" in template:
        return template.replace("{{dialog}}", dialog)
    return template + "\n\n" + dialog


def _build_incremental_prompt(dialog: str, refs: str, supplement: str) -> str:
    template = core.load_prompt(INCREMENTAL_PROMPT_FILE)
    return (
        template.replace("{{references}}", refs or "（无）")
        .replace("{{supplement}}", supplement or "（暂无）")
        .replace("{{dialog}}", dialog)
    )


def _friendly_db_error(err: str) -> str:
    low = err.lower()
    if "can't connect" in low or "connection refused" in low or "10061" in err:
        return "无法连接 MySQL 服务器，请检查服务器是否在线、端口是否开放、.env 配置是否正确"
    if "lost connection" in low or "server has gone away" in low or "2013" in err:
        return "MySQL 连接在分析过程中断开（可能因 AI 耗时较长），已自动重试；若仍失败请稍后重跑未学习客户"
    return err


def _learn_dialog_limit(cfg: dict) -> int:
    return int(cfg.get("deal_learn_max_dialog_chars") or 8000)


def _run_full_learn(dialog: str, cfg: dict) -> dict:
    user_prompt = _build_deal_user_prompt(dialog)
    system_prompt = (
        "你是跨境销售成交客户心理分析专家。"
        "请根据用户提供的完整聊天记录输出严格 JSON，不要 Markdown，不要解释。"
    )
    result = core.call_llm_prompt(cfg, system_prompt, user_prompt)
    if isinstance(result, dict) and not result.get("_错误"):
        result["_学习模式"] = "全量复盘"
    return result


def _run_incremental_learn(dialog: str, assessment: dict, cfg: dict) -> dict:
    refs = assessment.get("ref_compact") or ""
    supplement = assessment.get("supplement_excerpt") or ""
    user_prompt = _build_incremental_prompt(dialog, refs, supplement)
    system_prompt = (
        "你是跨境销售成交客户心理分析专家。"
        "请对照参考案例输出严格 JSON，不要 Markdown，不要解释。"
    )
    limit = _learn_dialog_limit(cfg)
    result = core.call_llm_prompt(cfg, system_prompt, user_prompt, max_chars=limit)
    if isinstance(result, dict) and not result.get("_错误"):
        mode = (result.get("学习模式") or "").strip()
        result["_学习模式"] = mode if mode else "增量更新"
    return result


def analyze_deal_customer(customer: dict, cfg: dict) -> dict:
    """对单个成交客户做心理分析（两阶段），异常写入返回 dict。"""
    customer_id = customer["customer_id"]
    session_id = customer.get("session_id")
    try:
        dialog = get_customer_full_dialog(customer_id)
        if not dialog.strip():
            return {"_错误": "该客户无可用聊天记录", "_学习模式": "失败"}

        # 阶段一：检索已有分析，判断 skip / incremental / full
        assessment = assess_deal_learning_need(dialog, cfg)
        action = assessment.get("action") or "full"
        reason = assessment.get("reason") or ""

        if action == "skip":
            best = assessment.get("best_match") or {}
            result = inherited_analysis_from_ref(best, reason)
            save_deal_analysis(customer_id, session_id, result)
            return result

        # 阶段二：注入参考案例后再学习
        if action == "incremental":
            result = _run_incremental_learn(dialog, assessment, cfg)
        else:
            result = _run_full_learn(dialog, cfg)

        if isinstance(result, dict) and result.get("_错误"):
            return result

        if isinstance(result, dict):
            result.setdefault("_学习模式", "全量复盘" if action == "full" else "增量更新")
            if reason and not str(result.get("成交阶段总结") or "").startswith("【"):
                result["成交阶段总结"] = f"【{result['_学习模式']}】{reason}\n" + str(
                    result.get("成交阶段总结") or ""
                )

        save_deal_analysis(customer_id, session_id, result)
        return result
    except Exception as e:
        err = _friendly_db_error(core.redact_secrets(str(e), cfg))
        logger.exception("deal analysis failed for customer %s", customer_id)
        return {"_错误": err, "_学习模式": "失败"}


def _extract_learn_mode(result: dict) -> str:
    mode = (result.get("_学习模式") or result.get("学习模式") or "").strip()
    if "继承" in mode or mode == "继承跳过":
        return "skipped"
    if "增量" in mode or mode == "确认复用":
        return "incremental"
    if mode == "全量复盘":
        return "full"
    if result.get("_错误"):
        return "failed"
    return "full"


def analyze_unlearned_deals(
    cfg: dict,
    limit: int = 20,
    on_progress=None,
) -> dict:
    """分析尚未做过成交心理学习的客户。

    on_progress(done, total, contact_name, success, mode) 可选回调。
    mode: skipped / incremental / full / failed
    """
    init_db()
    customers = get_unanalyzed_deal_customers(limit=limit)
    total = len(customers)
    success = 0
    failed = 0
    skipped = 0
    incremental = 0
    full = 0
    errors = []
    learned_items = []

    for i, cust in enumerate(customers, start=1):
        name = cust.get("contact_name") or cust.get("original_session_id")
        result = analyze_deal_customer(cust, cfg)
        mode = _extract_learn_mode(result)

        if mode == "failed":
            failed += 1
            errors.append(f"{name}: {result.get('_错误', '未知错误')}")
            if on_progress:
                on_progress(i, total, name, False, "failed")
        else:
            success += 1
            if mode == "skipped":
                skipped += 1
            elif mode == "incremental":
                incremental += 1
            else:
                full += 1

            rule = (result.get("智能体判断规则") or result.get("recommended_agent_rules") or "").strip()
            if rule:
                learned_items.append({
                    "contact_name": name,
                    "learn_mode": result.get("_学习模式") or mode,
                    "main_objection": result.get("主要顾虑") or result.get("main_objection") or "",
                    "recommended_agent_rules": rule,
                })
                logger.info(
                    "成交学习 · %s · [%s] · 规则：%s",
                    name,
                    result.get("_学习模式") or mode,
                    rule[:200],
                )
            if on_progress:
                on_progress(i, total, name, True, mode)

    summary = (
        f"待分析 {total}，成功 {success}（跳过 {skipped}，增量 {incremental}，全量 {full}），失败 {failed}"
    )
    if errors:
        summary += "；失败详情：" + "; ".join(errors[:5])

    try:
        save_daily_learning_run(
            run_date=date.today(),
            new_deal_customers=total,
            analyzed_customers=success,
            failed_customers=failed,
            summary=summary,
        )
    except Exception:
        logger.exception("save_daily_learning_run failed")

    return {
        "total": total,
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "incremental": incremental,
        "full": full,
        "errors": errors,
        "learned_items": learned_items,
    }
