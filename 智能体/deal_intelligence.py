# -*- coding: utf-8 -*-
"""成交客户心理分析 · 每日学习任务。"""

from __future__ import annotations

import logging
from datetime import date

import qc_core as core
from db import (
    get_customer_full_dialog,
    get_unanalyzed_deal_customers,
    save_deal_analysis,
    save_daily_learning_run,
)

logger = logging.getLogger(__name__)

DEAL_PROMPT_FILE = "deal_learning.md"


def load_deal_prompt() -> str:
    return core.load_prompt(DEAL_PROMPT_FILE)


def _build_deal_user_prompt(dialog: str) -> str:
    template = load_deal_prompt()
    if "{{dialog}}" in template:
        return template.replace("{{dialog}}", dialog)
    return template + "\n\n" + dialog


def analyze_deal_customer(customer: dict, cfg: dict) -> dict:
    """对单个成交客户做心理分析，异常写入返回 dict。"""
    customer_id = customer["customer_id"]
    session_id = customer.get("session_id")
    dialog = get_customer_full_dialog(customer_id)
    if not dialog.strip():
        return {"_错误": "该客户无可用聊天记录"}

    user_prompt = _build_deal_user_prompt(dialog)
    system_prompt = (
        "你是跨境销售成交客户心理分析专家。"
        "请根据用户提供的完整聊天记录输出严格 JSON，不要 Markdown，不要解释。"
    )
    try:
        result = core.call_llm_prompt(cfg, system_prompt, user_prompt)
        if isinstance(result, dict) and result.get("_错误"):
            return result
        save_deal_analysis(customer_id, session_id, result)
        return result
    except Exception as e:
        err = core.redact_secrets(str(e), cfg)
        logger.exception("deal analysis failed for customer %s", customer_id)
        return {"_错误": err}


def analyze_unlearned_deals(cfg: dict, limit: int = 20) -> dict:
    """分析尚未做过成交心理学习的客户。"""
    customers = get_unanalyzed_deal_customers(limit=limit)
    total = len(customers)
    success = 0
    failed = 0
    errors = []
    learned_items = []

    for cust in customers:
        result = analyze_deal_customer(cust, cfg)
        if isinstance(result, dict) and result.get("_错误"):
            failed += 1
            name = cust.get("contact_name") or cust.get("original_session_id")
            errors.append(f"{name}: {result['_错误']}")
        else:
            success += 1
            name = cust.get("contact_name") or cust.get("original_session_id")
            rule = (result.get("智能体判断规则") or result.get("recommended_agent_rules") or "").strip()
            if rule:
                learned_items.append({
                    "contact_name": name,
                    "main_objection": result.get("主要顾虑") or result.get("main_objection") or "",
                    "recommended_agent_rules": rule,
                })
                logger.info("成交学习 · %s · 智能体规则：%s", name, rule[:200])

    summary = f"待分析 {total}，成功 {success}，失败 {failed}"
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
        "errors": errors,
        "learned_items": learned_items,
    }
