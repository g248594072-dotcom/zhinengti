# -*- coding: utf-8 -*-
"""成交规则 AI 提纯去重与分类（供知识库管理页使用）。"""

from __future__ import annotations

import logging
from typing import Any

import qc_core as core

logger = logging.getLogger(__name__)

REFINE_PROMPT_FILE = "deal_rules_refine.md"


def _rules_to_numbered_text(rules: list[str]) -> str:
    lines = []
    for i, rule in enumerate(rules, 1):
        text = (rule or "").strip()
        if text:
            lines.append(f"{i}. {text}")
    return "\n\n".join(lines)


def _pick(d: dict, *keys: str, default: Any = "") -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def normalize_refine_result(raw: dict, source_count: int) -> dict:
    """校验并规范化 LLM 返回结构。"""
    if not isinstance(raw, dict):
        return {"ok": False, "error": "API 返回格式无效", "raw": raw}

    if raw.get("_错误"):
        return {"ok": False, "error": str(raw["_错误"]), "raw": raw}

    categories_in = raw.get("分类列表") or raw.get("categories") or []
    if not isinstance(categories_in, list):
        categories_in = []

    categories: list[dict] = []
    total_rules = 0

    for idx, cat in enumerate(categories_in):
        if not isinstance(cat, dict):
            continue
        cat_id = str(_pick(cat, "分类ID", "category_id", "id", default=f"cat_{idx + 1}")).strip()
        if not cat_id:
            cat_id = f"cat_{idx + 1}"

        entries_in = _pick(cat, "规则条目", "rules", "items", default=[])
        if not isinstance(entries_in, list):
            entries_in = []

        entries: list[dict] = []
        for j, item in enumerate(entries_in):
            if not isinstance(item, dict):
                if isinstance(item, str) and item.strip():
                    entries.append({
                        "规则ID": f"{cat_id}_{j + 1}",
                        "合并自序号": [],
                        "规则文本": item.strip(),
                    })
                continue
            text = str(_pick(item, "规则文本", "rule_text", "text", default="")).strip()
            if not text:
                continue
            src = _pick(item, "合并自序号", "source_indices", "merged_from", default=[])
            if not isinstance(src, list):
                src = []
            entries.append({
                "规则ID": str(_pick(item, "规则ID", "rule_id", "id", default=f"{cat_id}_{j + 1}")).strip(),
                "合并自序号": src,
                "规则文本": text,
            })

        if not entries:
            continue

        default_on = _pick(cat, "默认勾选", "default_selected", "selected", default=True)
        if isinstance(default_on, str):
            default_on = default_on.strip().lower() in ("true", "是", "yes", "1")

        categories.append({
            "分类ID": cat_id,
            "分类标题": str(_pick(cat, "分类标题", "category_title", "title", default=f"分类 {idx + 1}")).strip(),
            "分类说明": str(_pick(cat, "分类说明", "category_desc", "description", default="")).strip(),
            "默认勾选": bool(default_on),
            "规则条目": entries,
        })
        total_rules += len(entries)

    if not categories:
        return {
            "ok": False,
            "error": "未能解析出任何分类或规则，请重试或检查原始内容",
            "raw": raw,
        }

    deduped = _pick(raw, "去重后条数", "deduped_count", default=total_rules)
    try:
        deduped = int(deduped)
    except (TypeError, ValueError):
        deduped = total_rules

    return {
        "ok": True,
        "汇总说明": str(_pick(raw, "汇总说明", "summary", default="")).strip(),
        "原始条数": source_count,
        "去重后条数": deduped,
        "分类列表": categories,
        "raw": raw,
    }


def refine_rules(rules: list[str], cfg: dict) -> dict:
    """调用 LLM 对规则列表提纯、去重、分类。"""
    cleaned = [r.strip() for r in rules if (r or "").strip()]
    if not cleaned:
        return {"ok": False, "error": "没有可提纯的规则"}

    key = str(cfg.get("api_key") or "").strip()
    if not key or key == "YOUR_API_KEY_HERE":
        return {"ok": False, "error": "未配置 API Key"}

    try:
        template = core.load_prompt(REFINE_PROMPT_FILE)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}

    numbered = _rules_to_numbered_text(cleaned)
    user_prompt = template.replace("{{rules}}", numbered)
    system = "你是知识库管理员。严格按指令输出 JSON，不要输出其他内容。"

    # 规则列表不宜截断中间段，否则去重不完整
    raw = core.call_llm_prompt(
        cfg,
        system,
        user_prompt,
        max_chars=max(len(user_prompt) + 2000, 48000),
        truncate=False,
    )
    result = normalize_refine_result(raw, len(cleaned))
    if result.get("ok"):
        logger.info(
            "rule refine: %d -> %d rules, %d categories",
            len(cleaned),
            result.get("去重后条数"),
            len(result.get("分类列表") or []),
        )
    return result


def default_selected_ids(result: dict) -> set[str]:
    """返回默认应勾选的分类 ID。"""
    ids: set[str] = set()
    for cat in result.get("分类列表") or []:
        if cat.get("默认勾选"):
            ids.add(str(cat.get("分类ID")))
    return ids


def collect_rules_from_categories(
    result: dict,
    enabled_category_ids: set[str] | list[str],
) -> list[str]:
    """从勾选的分类中收集规则文本（保持分类内顺序）。"""
    enabled = set(enabled_category_ids)
    rules: list[str] = []
    seen: set[str] = set()
    for cat in result.get("分类列表") or []:
        if str(cat.get("分类ID")) not in enabled:
            continue
        for item in cat.get("规则条目") or []:
            text = str(item.get("规则文本") or "").strip()
            if text and text not in seen:
                rules.append(text)
                seen.add(text)
    return rules


def refine_stats(result: dict, enabled_category_ids: set[str] | None = None) -> dict:
    """统计信息供 UI 展示。"""
    cats = result.get("分类列表") or []
    enabled = enabled_category_ids if enabled_category_ids is not None else default_selected_ids(result)
    selected_rules = collect_rules_from_categories(result, enabled)
    return {
        "原始条数": result.get("原始条数", 0),
        "去重后条数": result.get("去重后条数", 0),
        "分类数": len(cats),
        "已勾选分类数": sum(1 for c in cats if str(c.get("分类ID")) in enabled),
        "将写入条数": len(selected_rules),
    }
