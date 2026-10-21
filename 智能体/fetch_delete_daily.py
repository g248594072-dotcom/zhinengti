# -*- coding: utf-8 -*-
"""
从 SaleSmartly 先筛删除标签，再筛时间范围内有客户沟通、完整历史客户有效发言 >6 的聊天 → 写入 MySQL。

建议 cron（在 daily_delete_job.py 流失复盘之前）：
    15 2 * * * cd /path/to/智能体 && python3 fetch_delete_daily.py

用法：
    python fetch_delete_daily.py
    python fetch_delete_daily.py --dry-run
    python fetch_delete_daily.py --date 2026-07-09
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

_LOG_DIR = os.path.join(_APP_DIR, "logs")
def _configure_logging():
    os.makedirs(_LOG_DIR, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(_LOG_DIR, "fetch_delete_daily.log"), encoding="utf-8"),
        ],
    )


logger = logging.getLogger(__name__)


def _parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def _salesmartly_settings(import_cfg: dict) -> dict:
    sm = import_cfg.get("salesmartly") or {}
    return {
        "delete_tag_keywords": sm.get("delete_tag_keywords") or ["删", "删除", "拉黑", "拒收"],
        "blacklist_tag_keywords": sm.get("blacklist_tag_keywords") or ["黑粉"],
        "date_label_category": sm.get("date_label_category") or "澳大利亚",
        "date_label_name": sm.get("date_label_name") or "日期",
    }


def run_fetch_delete_daily(
    *,
    target_day: datetime | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    dry_run: bool = False,
    on_progress=None,
    cancel_check=None,
) -> dict:
    from deal_import_core import get_mysql_target_label, load_import_config
    from delete_import_core import execute_delete_import
    from fetch_deal_salesmartly import yesterday_for_run
    from fetch_delete_salesmartly import fetch_delete_dataframe
    from fetch_cancel import FetchCancelledError, raise_if_cancelled
    from salesmartly_client import ConfigError, SaleSmartlyClient, load_config

    import_cfg = load_import_config()
    sm_settings = _salesmartly_settings(import_cfg)
    if (window_start is None) != (window_end is None):
        raise ValueError("请同时提供开始和结束时间。")
    if target_day is not None and window_start is not None:
        raise ValueError("业务日与时间范围不能同时指定。")
    day = (target_day or yesterday_for_run()).replace(hour=0, minute=0, second=0, microsecond=0)
    start = window_start if window_start is not None else day
    end = window_end if window_end is not None else day + timedelta(days=1)
    if start >= end:
        raise ValueError("结束时间必须晚于开始时间。")

    out = {
        "ok": False,
        "dry_run": dry_run,
        "target": get_mysql_target_label(import_cfg),
        "target_day": start.strftime("%Y-%m-%d"),
        "window_label": f"{start:%Y-%m-%d %H:%M} 至 {end:%Y-%m-%d %H:%M}（不含结束时刻）",
        "preview": [],
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

        df, meta = fetch_delete_dataframe(
            client,
            window_start=start,
            window_end=end,
            delete_keywords=sm_settings["delete_tag_keywords"],
            blacklist_keywords=sm_settings["blacklist_tag_keywords"],
            on_progress=_report_progress,
            cancel_check=cancel_check,
        )
    except FetchCancelledError:
        raise
    except Exception as e:
        out["error"] = f"SaleSmartly 拉取失败：{e}"
        logger.exception(out["error"])
        return out

    out["meta"] = meta
    out["preview"] = df.drop(columns=["会话消息内容"], errors="ignore").to_dict("records")
    source_name = f"SaleSmartly-删除-{start:%Y%m%d%H%M}-{end:%Y%m%d%H%M}"

    if df.empty:
        out["ok"] = True
        logger.info("选定范围内没有满足客户发言至少 7 条的删除客户（%s）", out["window_label"])
        return out

    if dry_run:
        out["ok"] = True
        out["summary"] = {
            "session_count": len(df),
            "delete_contacts": meta.get("delete_contacts", 0),
            "dry_run": True,
        }
        logger.info(
            "dry-run：%s 共 %d 通会话（删除客户 %d 人）",
            out["target_day"],
            len(df),
            meta.get("delete_contacts", 0),
        )
        return out

    raise_if_cancelled(cancel_check)
    try:
        result = execute_delete_import(
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
        "导入完成：成功 %s，标记已删除 %s",
        out["summary"].get("settlement", {}).get("success_count")
        or out["summary"].get("session_count"),
        out["summary"].get("marked_deleted", 0),
    )
    return out


def main() -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description="按沟通时间范围拉取删除客户并入库（客户有效发言至少 7 条）")
    parser.add_argument("--dry-run", action="store_true", help="只拉取预览，不写数据库")
    parser.add_argument("--date", type=str, default="", help="指定业务日 YYYY-MM-DD（默认昨天）")
    parser.add_argument("--start", default="", help="开始时间，如 2026-10-01T00:00:00")
    parser.add_argument("--end", default="", help="结束时间（不含），如 2026-10-22T00:00:00")
    args = parser.parse_args()
    if bool(args.start) != bool(args.end):
        parser.error("--start 和 --end 必须同时提供")
    if args.date and args.start:
        parser.error("--date 与 --start/--end 不能同时使用")

    target_day = _parse_date(args.date) if args.date else None
    try:
        result = run_fetch_delete_daily(
            target_day=target_day, dry_run=args.dry_run,
            window_start=datetime.fromisoformat(args.start) if args.start else None,
            window_end=datetime.fromisoformat(args.end) if args.end else None,
        )
    except ValueError as exc:
        parser.error(str(exc))

    print(f"目标库：{result.get('target')}")
    print(f"沟通时间范围：{result.get('window_label')}")
    meta = result.get("meta") or {}
    if meta:
        print(
            f"删除标签 {len(meta.get('delete_labels', []))} 个 · "
            f"标签下 {meta.get('contacts_total', 0)} 人 · "
            f"删除 {meta.get('delete_contacts', 0)} 人 · "
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
    print(f"标记已删除：{summary.get('marked_deleted', 0)}")
    _notify(result, failed=False)
    return 0


def _notify(result: dict, *, failed: bool) -> None:
    try:
        from feishu_notify import notify_fetch_delete_daily_result
    except ImportError:
        return
    log_path = os.path.join(_LOG_DIR, "fetch_delete_daily.log")
    notify_fetch_delete_daily_result(result, failed=failed, log_path=log_path)


if __name__ == "__main__":
    sys.exit(main())
