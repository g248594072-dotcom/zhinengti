# -*- coding: utf-8 -*-
"""
每日从 SaleSmartly 拉取昨日成交客户 → 写入 MySQL → 标记已成交。

建议 cron（在 daily_job.py 心理学习之前）：
    0 2 * * * cd /path/to/智能体 && python3 fetch_deal_daily.py

用法：
    python fetch_deal_daily.py
    python fetch_deal_daily.py --dry-run
    python fetch_deal_daily.py --date 2026-07-09
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

_LOG_DIR = os.path.join(_APP_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(_LOG_DIR, "fetch_deal_daily.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def _parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def _salesmartly_settings(import_cfg: dict) -> dict:
    sm = import_cfg.get("salesmartly") or {}
    return {
        "deal_tag_keywords": sm.get("deal_tag_keywords") or ["全款", "定金", "分期"],
        "date_label_category": sm.get("date_label_category") or "澳大利亚",
        "date_label_name": sm.get("date_label_name") or "日期",
    }


def run_fetch_deal_daily(
    *,
    target_day: datetime | None = None,
    dry_run: bool = False,
    on_progress=None,
) -> dict:
    from deal_import_core import execute_deal_import, get_mysql_target_label, load_import_config
    from fetch_deal_salesmartly import fetch_yesterday_deal_dataframe, yesterday_for_run
    from salesmartly_client import ConfigError, SaleSmartlyClient, load_config

    import_cfg = load_import_config()
    sm_settings = _salesmartly_settings(import_cfg)
    day = target_day or yesterday_for_run()

    out = {
        "ok": False,
        "dry_run": dry_run,
        "target": get_mysql_target_label(import_cfg),
        "target_day": day.strftime("%Y-%m-%d"),
        "meta": {},
        "summary": {},
        "error": "",
    }

    try:
        ss_cfg = load_config()
        client = SaleSmartlyClient(ss_cfg)
    except ConfigError as e:
        out["error"] = str(e)
        logger.error(out["error"])
        return out

    try:
        def _report_progress(msg: str) -> None:
            if on_progress:
                on_progress(msg)
            logger.info(msg)

        df, meta = fetch_yesterday_deal_dataframe(
            client,
            target_day=day,
            deal_keywords=sm_settings["deal_tag_keywords"],
            date_label_category=sm_settings["date_label_category"],
            date_label_name=sm_settings["date_label_name"],
            on_progress=_report_progress,
        )
    except Exception as e:
        out["error"] = f"SaleSmartly 拉取失败：{e}"
        logger.exception(out["error"])
        return out

    out["meta"] = meta
    source_name = f"SaleSmartly-{meta.get('date_tag', day.strftime('%Y-%m-%d'))}"

    if df.empty:
        out["ok"] = True
        out["error"] = ""
        logger.info("昨日无成交客户或无可导入会话（%s）", out["target_day"])
        return out

    if dry_run:
        out["ok"] = True
        out["summary"] = {
            "session_count": len(df),
            "deal_contacts": meta.get("deal_contacts", 0),
            "dry_run": True,
        }
        logger.info(
            "dry-run：%s 共 %d 通会话（成交客户 %d 人）",
            out["target_day"],
            len(df),
            meta.get("deal_contacts", 0),
        )
        return out

    try:
        result = execute_deal_import(
            file_dfs=[(source_name, df)],
            run_analyze=False,
        )
    except Exception as e:
        out["error"] = f"入库失败：{e}"
        logger.exception(out["error"])
        return out

    out["summary"] = result.get("summary") or {}
    if not result.get("ok"):
        out["error"] = result.get("error") or "导入失败"
        logger.error(out["error"])
        return out

    out["ok"] = True
    logger.info(
        "导入完成：成功 %s，标记已成交 %s",
        out["summary"].get("settlement", {}).get("success_count")
        or out["summary"].get("session_count"),
        out["summary"].get("marked_deal", 0),
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="每日拉取昨日成交客户并入库")
    parser.add_argument("--dry-run", action="store_true", help="只拉取预览，不写数据库")
    parser.add_argument("--date", type=str, default="", help="指定业务日 YYYY-MM-DD（默认昨天）")
    args = parser.parse_args()

    target_day = _parse_date(args.date) if args.date else None
    result = run_fetch_deal_daily(target_day=target_day, dry_run=args.dry_run)

    print(f"目标库：{result.get('target')}")
    print(f"业务日：{result.get('target_day')}")
    meta = result.get("meta") or {}
    if meta:
        print(
            f"日期标签 {meta.get('date_tag')} · "
            f"标签下 {meta.get('contacts_total', 0)} 人 · "
            f"成交 {meta.get('deal_contacts', 0)} 人 · "
            f"有效会话 {meta.get('sessions_with_messages', 0)}"
        )

    if result.get("error") and not result.get("ok"):
        print(f"失败：{result['error']}")
        _notify(result, failed=True)
        return 1

    if args.dry_run:
        print("(dry-run，未写入数据库)")
        return 0

    summary = result.get("summary") or {}
    settlement = summary.get("settlement") or {}
    print(f"成功入库：{settlement.get('success_count', summary.get('session_count', 0))}")
    print(f"标记已成交：{summary.get('marked_deal', 0)}")
    _notify(result, failed=False)
    return 0


def _notify(result: dict, *, failed: bool) -> None:
    try:
        from feishu_notify import notify_fetch_deal_daily_result
    except ImportError:
        return
    log_path = os.path.join(_LOG_DIR, "fetch_deal_daily.log")
    notify_fetch_deal_daily_result(result, failed=failed, log_path=log_path)


if __name__ == "__main__":
    sys.exit(main())
