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
_OUTPUT_DIR = os.path.join(os.path.dirname(_APP_DIR), "输出结果")

# 兼容旧内部名
_SUPPLEMENT_FILE = SUPPLEMENT_FILE


def _ensure_dirs() -> None:
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    os.makedirs(_PROMPTS_DIR, exist_ok=True)


def _clip(text: str, n: int = 400) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else t[: n - 3] + "..."


def fetch_rules(days: int = 7) -> list[dict]:
    from db import init_db, list_deal_analyses_since_days

    init_db()
    deals = list_deal_analyses_since_days(days=days)
    rules = []
    seen: set[str] = set()
    for deal in deals:
        rule = (deal.get("recommended_agent_rules") or "").strip()
        if not rule or rule in seen:
            continue
        seen.add(rule)
        rules.append({
            "contact_name": deal.get("contact_name") or deal.get("original_session_id") or "?",
            "analyzed_at": deal.get("analyzed_at"),
            "main_objection": deal.get("main_objection") or "",
            "reusable_sales_experience": deal.get("reusable_sales_experience") or "",
            "recommended_agent_rules": rule,
        })
    return rules


def build_review_markdown(rules: list[dict], days: int) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 待审核成交规则汇总（最近 {days} 天）",
        f"",
        f"生成时间：{now}",
        f"共 {len(rules)} 条不重复规则。",
        "",
        "## 使用说明",
        "1. 逐条审核下方「智能体判断规则」，删除不适用的条目。",
        "2. 可将保留的规则复制到单独文件，执行：",
        "   `python weekly_prompt_merge.py --apply --input <你的文件>`",
        "3. 合并后 `prompts/deal_learned_supplement.md` 会在质检时自动附加到 full/lite prompt。",
        "",
        "---",
        "",
    ]
    for i, item in enumerate(rules, 1):
        at = item.get("analyzed_at")
        at_str = at.strftime("%Y-%m-%d") if hasattr(at, "strftime") else str(at or "")
        lines.extend([
            f"### 规则 {i} · {item.get('contact_name')} · {at_str}",
            f"- 主要顾虑：{_clip(str(item.get('main_objection')), 120)}",
            f"- 可复用经验：{_clip(str(item.get('reusable_sales_experience')), 200)}",
            "",
            "**智能体判断规则（审核后保留）**",
            "",
            str(item.get("recommended_agent_rules")),
            "",
            "---",
            "",
        ])
    return "\n".join(lines)


def export_rules_markdown(days: int = 7) -> tuple[list[dict], str]:
    """从数据库拉取规则并生成待审核 Markdown。"""
    rules = fetch_rules(days=days)
    md = build_review_markdown(rules, days)
    return rules, md


def read_current_supplement() -> str:
    if os.path.isfile(SUPPLEMENT_FILE):
        with open(SUPPLEMENT_FILE, encoding="utf-8") as f:
            return f.read()
    return ""


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
) -> dict:
    """将规则写入 deal_learned_supplement.md。"""
    _ensure_dirs()
    cleaned = [r.strip() for r in rules if (r or "").strip()]
    if merge_existing:
        existing = extract_rules_from_text(read_current_supplement())
        seen = set(existing)
        merged = list(existing)
        for rule in cleaned:
            if rule not in seen:
                merged.append(rule)
                seen.add(rule)
        cleaned = merged
    if not cleaned:
        return {"ok": False, "error": "没有可写入的规则", "count": 0, "path": SUPPLEMENT_FILE}
    md = build_supplement_markdown(cleaned)
    with open(SUPPLEMENT_FILE, "w", encoding="utf-8") as f:
        f.write(md)
    return {
        "ok": True,
        "count": len(cleaned),
        "path": SUPPLEMENT_FILE,
        "preview": md,
    }


def build_supplement_markdown(rules: list[str]) -> str:
    now = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"<!-- 由 weekly_prompt_merge.py 生成/更新 · {now} -->",
        f"<!-- 人工审核后的成交复盘规则；质检 full/lite 模式自动加载 -->",
        "",
    ]
    for i, rule in enumerate(rules, 1):
        lines.append(f"## 规则 {i}")
        lines.append(rule.strip())
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def cmd_export(days: int, dry_run: bool) -> str:
    _ensure_dirs()
    rules, md = export_rules_markdown(days=days)
    if dry_run:
        print(md[:3000])
        if len(md) > 3000:
            print("\n…（dry-run 仅预览前 3000 字）")
        return ""
    out_name = f"待审核成交规则-{datetime.now().strftime('%Y%m%d%H%M%S')}.md"
    out_path = os.path.join(_OUTPUT_DIR, out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"已导出 {len(rules)} 条规则 → {out_path}")
    return out_path


def cmd_apply(input_path: str, dry_run: bool, merge_existing: bool = False) -> None:
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
            existing = extract_rules_from_text(read_current_supplement())
            seen = set(existing)
            preview_rules = list(existing)
            for r in rules:
                if r not in seen:
                    preview_rules.append(r)
        print(build_supplement_markdown(preview_rules))
        return
    result = apply_rules_to_supplement(rules, merge_existing=merge_existing)
    if not result.get("ok"):
        print(result.get("error") or "写入失败")
        sys.exit(1)
    print(f"已合并 {result['count']} 条规则 → {result['path']}")


def main():
    parser = argparse.ArgumentParser(description="成交规则周汇总与 prompt 合并")
    parser.add_argument("--export", action="store_true", help="导出待审核规则 markdown")
    parser.add_argument("--apply", action="store_true", help="将审核后规则写入 deal_learned_supplement.md")
    parser.add_argument("--input", type=str, default="", help="--apply 时指定审核文件路径")
    parser.add_argument("--days", type=int, default=7, help="导出最近 N 天（默认 7）")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写文件")
    args = parser.parse_args()

    if args.export:
        cmd_export(days=args.days, dry_run=args.dry_run)
    elif args.apply:
        if not args.input:
            print("请使用 --input 指定审核后的规则文件")
            sys.exit(1)
        cmd_apply(args.input, dry_run=args.dry_run)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
