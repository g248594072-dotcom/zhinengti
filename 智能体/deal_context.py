# -*- coding: utf-8 -*-
"""成交经验检索 · 质检 prompt 注入 · 相似客户匹配。"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# 与 full_qc.md 中「客户主要顾虑」对齐
CONCERN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "效果": (
        "效果", "有用吗", "管用", "见效", "works", "work", "effective", "result",
        "improve", "harder", "longer", "size", "erection",
    ),
    "信任": (
        "骗", " scam", "fake", "trust", "legit", "certificate", "资质", "同仁堂",
        "doctor", "tina", "ahpra", "registered",
    ),
    "价格": (
        "贵", "价格", "多少钱", "price", "cost", "aud", "dollar", "$", "expensive",
        "afford", "discount", "优惠",
    ),
    "拖延": (
        "考虑", "想想", "later", "think about", "not now", "next week", "明天", "下周",
    ),
    "COD": (
        "cod", "cash on delivery", "货到付款", "到货付款", "pay on delivery",
    ),
}

STAGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "进线": ("hi", "hello", "hey", "good morning", "interested", "进线"),
    "问诊": ("symptom", "1-5", "1.", "2.", "3.", "4.", "5.", "问诊", "硬度", "早泄", "前列腺"),
    "报价前": ("方案", "treatment", "custom", "成分", "浓缩", "before you decide"),
    "已报价": (
        "aud", "original price", "limited-time", "plan a", "plan b", "优惠价",
        "800", "400", "600", "300",
    ),
    "已成交": ("付款", "支付", "地址", "下单", "paid", "payment", "address", "order"),
}

_COMPARE_FIELDS = (
    "结果标签", "客户阶段", "客户主要顾虑", "是否合格",
    "风险等级", "主要问题", "改进建议", "下一步跟进动作",
)


def _norm(text: str) -> str:
    return (text or "").lower()


def infer_dialog_signals(dialog: str) -> dict:
    """从聊天文本启发式推断阶段与顾虑（供相似匹配，非最终质检结论）。"""
    text = _norm(dialog)
    concerns = [name for name, kws in CONCERN_KEYWORDS.items() if any(k in text for k in kws)]
    stages = [name for name, kws in STAGE_KEYWORDS.items() if any(k in text for k in kws)]

    stage = stages[-1] if stages else "未知"
    concern = concerns[0] if concerns else "无"

    cust_lines = []
    for line in (dialog or "").splitlines():
        if re.search(r"客户\s*[:：]", line):
            cust_lines.append(line)
    snippet = "\n".join(cust_lines[-8:]) if cust_lines else (dialog or "")[-1200:]

    return {
        "stage": stage,
        "concern": concern,
        "concerns": concerns,
        "stages": stages,
        "customer_snippet": snippet,
    }


def _token_overlap(a: str, b: str) -> int:
    if not a or not b:
        return 0
    ta = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z]{3,}", _norm(a)))
    tb = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z]{3,}", _norm(b)))
    if not ta or not tb:
        return 0
    return len(ta & tb)


def score_deal_relevance(dialog: str, signals: dict, deal: dict) -> float:
    """为单条成交记录打分，越高越相似。"""
    score = 0.0
    concern = signals.get("concern") or "无"
    deal_objection = str(deal.get("main_objection") or "")
    deal_profile = str(deal.get("customer_profile") or "")
    deal_summary = str(deal.get("deal_stage_summary") or "")
    deal_psych = str(deal.get("customer_psychology_path") or "")
    deal_blob = f"{deal_objection} {deal_profile} {deal_summary} {deal_psych}"

    if concern != "无" and concern in deal_objection:
        score += 4.0
    for c in signals.get("concerns") or []:
        if c in deal_blob:
            score += 1.5

    stage = signals.get("stage") or ""
    if stage and stage in deal_summary:
        score += 2.0
    for s in signals.get("stages") or []:
        if s in deal_summary:
            score += 0.8

    snippet = signals.get("customer_snippet") or dialog
    score += min(_token_overlap(snippet, deal_profile) * 0.6, 3.0)
    score += min(_token_overlap(dialog[-2000:], deal_psych) * 0.4, 2.0)

    analyzed_at = deal.get("analyzed_at")
    if isinstance(analyzed_at, datetime):
        if analyzed_at.date() == date.today():
            score += 2.0
        elif analyzed_at >= datetime.now() - timedelta(days=7):
            score += 1.0

    if str(deal.get("recommended_agent_rules") or "").strip():
        score += 0.5
    if str(deal.get("reusable_sales_experience") or "").strip():
        score += 0.5

    return score


def _load_all_deals() -> list[dict]:
    try:
        from db import list_deal_analyses

        return list_deal_analyses(limit=300)
    except Exception as e:
        logger.warning("加载成交分析失败，跳过经验注入: %s", e)
        return []


def find_similar_deals(
    dialog: str,
    limit: int = 5,
    prefer_today: bool = True,
) -> list[dict]:
    """按阶段/顾虑/话术相似度检索成交案例。"""
    deals = _load_all_deals()
    if not deals:
        return []

    signals = infer_dialog_signals(dialog)
    scored: list[tuple[float, dict]] = []
    for deal in deals:
        s = score_deal_relevance(dialog, signals, deal)
        if s > 0:
            scored.append((s, deal))

    if not scored:
        # 无明确匹配时退回最近成交记录
        fallback = deals[:limit]
        for d in fallback:
            d["_match_score"] = 0.0
            d["_match_reason"] = "最近成交（无强匹配）"
        return fallback

    scored.sort(key=lambda x: (-x[0], str(x[1].get("analyzed_at") or "")))
    if prefer_today:
        today_hits = [(s, d) for s, d in scored if _is_today(d.get("analyzed_at"))]
        if today_hits:
            scored = today_hits + [(s, d) for s, d in scored if not _is_today(d.get("analyzed_at"))]

    out = []
    for s, deal in scored[: max(1, int(limit))]:
        item = dict(deal)
        item["_match_score"] = round(s, 2)
        item["_match_reason"] = _build_match_reason(signals, deal)
        out.append(item)
    return out


def _is_today(dt) -> bool:
    if isinstance(dt, datetime):
        return dt.date() == date.today()
    return False


def _build_match_reason(signals: dict, deal: dict) -> str:
    parts = []
    concern = signals.get("concern")
    if concern and concern in str(deal.get("main_objection") or ""):
        parts.append(f"顾虑≈{concern}")
    stage = signals.get("stage")
    if stage and stage in str(deal.get("deal_stage_summary") or ""):
        parts.append(f"阶段≈{stage}")
    if not parts:
        parts.append("话术/画像相近")
    return "、".join(parts)


def _clip(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 3] + "..."


def build_deal_reference_note(deals: list[dict]) -> str:
    """组装注入质检 prompt 的「参考成交案例」区块。"""
    if not deals:
        return ""

    lines = [
        "【参考成交案例 · 仅供对照判断，以下均为已成交客户的复盘经验】",
        "请结合当前会话证据使用：相似处可借鉴判断逻辑，不可生搬硬套。",
        "",
    ]
    for i, deal in enumerate(deals, 1):
        name = deal.get("contact_name") or deal.get("original_session_id") or f"案例{i}"
        reason = deal.get("_match_reason") or ""
        score = deal.get("_match_score")
        header = f"案例{i}：{name}"
        if reason:
            header += f"（{reason}"
            if score is not None:
                header += f"，匹配度{score}"
            header += "）"
        lines.append(header)
        if deal.get("customer_profile"):
            lines.append(f"· 客户画像：{_clip(str(deal['customer_profile']), 200)}")
        if deal.get("main_objection"):
            lines.append(f"· 主要顾虑：{_clip(str(deal['main_objection']), 120)}")
        if deal.get("reusable_sales_experience"):
            lines.append(f"· 可复用经验：{_clip(str(deal['reusable_sales_experience']), 280)}")
        if deal.get("recommended_agent_rules"):
            lines.append(f"· 智能体判断规则：{_clip(str(deal['recommended_agent_rules']), 320)}")
        lines.append("")

    return "\n".join(lines).strip()


def get_deal_context_for_qc(dialog: str, cfg: dict | None = None) -> tuple[str, list[dict]]:
    """质检前检索并生成参考案例 note；返回 (note, deals)。"""
    cfg = cfg or {}
    if cfg.get("deal_context_enabled") is False:
        return "", []

    limit = int(cfg.get("deal_context_limit") or 5)
    prefer_today = cfg.get("deal_context_prefer_today", True)
    deals = find_similar_deals(dialog, limit=limit, prefer_today=prefer_today)
    if not deals:
        return "", []
    return build_deal_reference_note(deals), deals


def summarize_deal_refs(deals: list[dict]) -> str:
    if not deals:
        return ""
    names = [
        str(d.get("contact_name") or d.get("original_session_id") or "?")
        for d in deals[:5]
    ]
    return f"{len(deals)}条：" + "、".join(names)


def diff_qc_rows(row_a: dict, row_b: dict, fields: tuple[str, ...] | None = None) -> list[dict]:
    """对比两次质检结果的关键字段差异（效果验证用）。"""
    fields = fields or _COMPARE_FIELDS
    diffs = []
    for key in fields:
        a = str(row_a.get(key, "") or "").strip()
        b = str(row_b.get(key, "") or "").strip()
        if a != b:
            diffs.append({"字段": key, "有经验参考": a, "无经验参考": b})
    return diffs
