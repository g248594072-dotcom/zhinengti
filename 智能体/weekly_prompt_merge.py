# -*- coding: utf-8 -*-
"""
每周汇总成交「智能体判断规则」→ 供人工审核后合并进质检标准。

用法：
  # 导出最近 7 天规则到待审核文件
  python weekly_prompt_merge.py --export --days 7

  # 将已审核的规则写入 prompts/deal_learned_supplement.md（质检时自动加载）
  python weekly_prompt_merge.py --apply --input 输出结果/待审核成交规则-20260706.md

  # 预览合并结果不写文件
  python weekly_prompt_merge.py --export --days 7 --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

_PROMPTS_DIR = os.path.join(_APP_DIR, "prompts")
SUPPLEMENT_FILE = os.path.join(_PROMPTS_DIR, "deal_learned_supplement.md")
DELETE_SUPPLEMENT_FILE = os.path.join(_PROMPTS_DIR, "delete_learned_supplement.md")
_OUTPUT_DIR = os.path.join(os.path.dirname(_APP_DIR), "输出结果")

# 兼容旧内部名
_SUPPLEMENT_FILE = SUPPLEMENT_FILE


def _ensure_dirs() -> None:
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    os.makedirs(_PROMPTS_DIR, exist_ok=True)


def _clip(text: str, n: int = 400) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else t[: n - 3] + "..."


def fetch_rules(days: int = 7, rule_type: str = "deal") -> list[dict]:
    from db import init_db, list_deal_analyses_since_days, list_delete_analyses_since_days

    init_db()
    if rule_type == "delete":
        records = list_delete_analyses_since_days(days=days)
        rule_field = "recommended_agent_rules"
        extra_fields = ("churn_pattern", "avoidable_experience", "warning_signals")
    else:
        records = list_deal_analyses_since_days(days=days)
        rule_field = "recommended_agent_rules"
        extra_fields = ("main_objection", "reusable_sales_experience")

    rules = []
    seen: set[str] = set()
    for rec in records:
        rule = (rec.get(rule_field) or "").strip()
        if not rule or rule in seen:
            continue
        seen.add(rule)
        item = {
            "contact_name": rec.get("contact_name") or rec.get("original_session_id") or "?",
            "analyzed_at": rec.get("analyzed_at"),
            "recommended_agent_rules": rule,
        }
        for f in extra_fields:
            item[f] = rec.get(f) or ""
        rules.append(item)
    return rules


def build_review_markdown(rules: list[dict], days: int, rule_type: str = "deal") -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    label = "删除" if rule_type == "delete" else "成交"
    supplement_name = (
        "delete_learned_supplement.md" if rule_type == "delete" else "deal_learned_supplement.md"
    )
    lines = [
        f"# 待审核{label}规则汇总（最近 {days} 天）",
        f"",
        f"生成时间：{now}",
        f"共 {len(rules)} 条不重复规则。",
        "",
        "## 使用说明",
        "1. 逐条审核下方「智能体判断规则」，删除不适用的条目。",
        "2. 可将保留的规则复制到单独文件，执行：",
        f"   `python weekly_prompt_merge.py --apply --type {rule_type} --input <你的文件>`",
        f"3. 合并后 `prompts/{supplement_name}` 会在质检时自动附加到 full/lite prompt。",
        "",
        "---",
        "",
    ]
    for i, item in enumerate(rules, 1):
        at = item.get("analyzed_at")
        at_str = at.strftime("%Y-%m-%d") if hasattr(at, "strftime") else str(at or "")
        lines.append(f"### 规则 {i} · {item.get('contact_name')} · {at_str}")
        if rule_type == "delete":
            lines.append(f"- 流失模式：{_clip(str(item.get('churn_pattern')), 120)}")
            lines.append(f"- 预警信号：{_clip(str(item.get('warning_signals')), 160)}")
            lines.append(f"- 可规避经验：{_clip(str(item.get('avoidable_experience')), 200)}")
        else:
            lines.append(f"- 主要顾虑：{_clip(str(item.get('main_objection')), 120)}")
            lines.append(f"- 可复用经验：{_clip(str(item.get('reusable_sales_experience')), 200)}")
        lines.extend([
            "",
            "**智能体判断规则（审核后保留）**",
            "",
            str(item.get("recommended_agent_rules")),
            "",
            "---",
            "",
        ])
    return "\n".join(lines)


def export_rules_markdown(days: int = 7, rule_type: str = "deal") -> tuple[list[dict], str]:
    rules = fetch_rules(days=days, rule_type=rule_type)
    md = build_review_markdown(rules, days, rule_type=rule_type)
    return rules, md


def read_current_supplement(rule_type: str = "deal") -> str:
    path = DELETE_SUPPLEMENT_FILE if rule_type == "delete" else SUPPLEMENT_FILE
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return ""


def count_rules_not_in_supplement(rule_type: str = "deal") -> dict:
    from db import list_distinct_agent_rules, list_distinct_delete_agent_rules

    if rule_type == "delete":
        in_db = list_distinct_delete_agent_rules()
        kb = set(extract_rules_from_text(read_current_supplement("delete")))
    else:
        in_db = list_distinct_agent_rules()
        kb = set(extract_rules_from_text(read_current_supplement("deal")))
    not_in_kb = [r for r in in_db if r not in kb]
    return {
        "db_distinct": len(in_db),
        "kb_written": len(kb),
        "pending": len(not_in_kb),
    }


def count_delete_rules_not_in_supplement() -> dict:
    return count_rules_not_in_supplement(rule_type="delete")


def extract_rules_from_text(content: str) -> list[str]:
    """从待审核 markdown 或纯文本中提取规则块。"""
    rules: list[str] = []
    blocks = re.split(r"(?m)^###\s+规则\s+\d+", content or "")
    for block in blocks[1:]:
        m = re.search(
            r"\*\*智能体判断规则[^*]*\*\*\s*\n+([\s\S]*?)(?=\n---|\n###|\Z)",
            block,
        )
        if m:
            rule = m.group(1).strip()
            if rule:
                rules.append(rule)
                continue
        lines = [ln.strip() for ln in block.splitlines() if ln.strip() and not ln.startswith("#")]
        if lines:
            rules.append("\n".join(lines).strip())

    if not rules and (content or "").strip():
        # 已是 supplement 格式（## 规则 N）
        blocks2 = re.split(r"(?m)^##\s+规则\s+\d+", content)
        for block in blocks2[1:]:
            body = block.strip()
            if body and not body.startswith("<!--"):
                rules.append(body.split("\n---")[0].strip())

    if not rules and (content or "").strip():
        rules.append(content.strip())
    return rules


def extract_rules_from_review_file(path: str) -> list[str]:
    """从待审核 markdown 或纯文本文件中提取规则块。"""
    with open(path, encoding="utf-8") as f:
        return extract_rules_from_text(f.read())


def apply_rules_to_supplement(
    rules: list[str],
    *,
    merge_existing: bool = False,
    rule_type: str = "deal",
) -> dict:
    """将规则写入 deal/delete_learned_supplement.md。"""
    _ensure_dirs()
    path = DELETE_SUPPLEMENT_FILE if rule_type == "delete" else SUPPLEMENT_FILE
    cleaned = [r.strip() for r in rules if (r or "").strip()]
    if merge_existing:
        existing = extract_rules_from_text(read_current_supplement(rule_type))
        seen = set(existing)
        merged = list(existing)
        for rule in cleaned:
            if rule not in seen:
                merged.append(rule)
                seen.add(rule)
        cleaned = merged
    if not cleaned:
        return {"ok": False, "error": "没有可写入的规则", "count": 0, "path": path}
    md = build_supplement_markdown(cleaned, rule_type=rule_type)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return {
        "ok": True,
        "count": len(cleaned),
        "path": path,
        "preview": md,
    }


def build_supplement_markdown(rules: list[str], rule_type: str = "deal") -> str:
    now = datetime.now().strftime("%Y-%m-%d")
    label = "删除复盘" if rule_type == "delete" else "成交复盘"
    lines = [
        f"<!-- 由 weekly_prompt_merge.py 生成/更新 · {now} -->",
        f"<!-- 人工审核后的{label}规则；质检 full/lite 模式自动加载 -->",
        "",
    ]
    for i, rule in enumerate(rules, 1):
        lines.append(f"## 规则 {i}")
        lines.append(rule.strip())
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def cmd_export(days: int, dry_run: bool, rule_type: str = "deal") -> str:
    _ensure_dirs()
    rules, md = export_rules_markdown(days=days, rule_type=rule_type)
    if dry_run:
        print(md[:3000])
        if len(md) > 3000:
            print("\n…（dry-run 仅预览前 3000 字）")
        return ""
    label = "删除" if rule_type == "delete" else "成交"
    out_name = f"待审核{label}规则-{datetime.now().strftime('%Y%m%d%H%M%S')}.md"
    out_path = os.path.join(_OUTPUT_DIR, out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"已导出 {len(rules)} 条规则 → {out_path}")
    return out_path


def cmd_apply(input_path: str, dry_run: bool, merge_existing: bool = False, rule_type: str = "deal") -> None:
    if not os.path.isfile(input_path):
        print(f"文件不存在：{input_path}")
        sys.exit(1)
    rules = extract_rules_from_review_file(input_path)
    if not rules:
        print("未从文件中解析到任何规则。")
        sys.exit(1)
    if dry_run:
        preview_rules = rules
        if merge_existing:
            existing = extract_rules_from_text(read_current_supplement(rule_type))
            seen = set(existing)
            preview_rules = list(existing)
            for r in rules:
                if r not in seen:
                    preview_rules.append(r)
        print(build_supplement_markdown(preview_rules, rule_type=rule_type))
        return
    result = apply_rules_to_supplement(rules, merge_existing=merge_existing, rule_type=rule_type)
    if not result.get("ok"):
        print(result.get("error") or "写入失败")
        sys.exit(1)
    print(f"已合并 {result['count']} 条规则 → {result['path']}")


def main():
    parser = argparse.ArgumentParser(description="成交/删除规则周汇总与 prompt 合并")
    parser.add_argument("--export", action="store_true", help="导出待审核规则 markdown")
    parser.add_argument("--apply", action="store_true", help="将审核后规则写入 supplement 文件")
    parser.add_argument("--type", choices=["deal", "delete"], default="deal", help="规则类型（默认 deal）")
    parser.add_argument("--input", type=str, default="", help="--apply 时指定审核文件路径")
    parser.add_argument("--days", type=int, default=7, help="导出最近 N 天（默认 7）")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写文件")
    parser.add_argument("--merge-existing", action="store_true", help="合并到现有知识库而非覆盖")
    args = parser.parse_args()

    if args.export:
        cmd_export(days=args.days, dry_run=args.dry_run, rule_type=args.type)
    elif args.apply:
        if not args.input:
            print("请使用 --input 指定审核后的规则文件")
            sys.exit(1)
        cmd_apply(args.input, dry_run=args.dry_run, merge_existing=args.merge_existing, rule_type=args.type)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
