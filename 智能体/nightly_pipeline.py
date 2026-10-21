# -*- coding: utf-8 -*-
"""北京时间每日02:00：成交/删除拉取入库、实际AI分析及历史继承修复。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")


def previous_day(now=None):
    current = now or datetime.now(BEIJING)
    if current.tzinfo is not None:
        current = current.astimezone(BEIJING)
    return current.date() - timedelta(days=1)


def days_to_import(last_day, target_day):
    start = date.fromisoformat(last_day) + timedelta(days=1) if last_day else target_day
    return [start + timedelta(days=i) for i in range(max(0, (target_day-start).days+1))]


def select_learning_customers(customers, repair_limit):
    fresh = [c for c in customers if not c.get("needs_repair")]
    repairs = [c for c in customers if c.get("needs_repair")]
    return fresh + repairs[:max(0, repair_limit)], len(repairs)


def _save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str)+"\n", encoding="utf-8")
    os.replace(temp, path)


@contextmanager
def pipeline_lock(config_dir):
    """OS释放锁，异常退出不留下需要人工清理的锁。"""
    path = Path(config_dir)/"nightly_pipeline.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_pipeline(*, target_day=None, repair_limit=50, config_dir=None):
    import qc_core as core
    from db import (get_unanalyzed_deal_customers, get_unanalyzed_delete_customers,
                    init_db, format_database_error)
    from fetch_deal_daily import run_fetch_deal_daily
    from fetch_delete_daily import run_fetch_delete_daily
    from deal_intelligence import analyze_unlearned_deals
    from delete_intelligence import analyze_unlearned_deletes

    target_day = target_day or previous_day()
    directory = Path(config_dir or core.get_config_dir())
    state_path = directory/"nightly_pipeline_state.json"
    with pipeline_lock(directory):
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        imported = state.setdefault("last_imported_day", {})
        result = {"started_at": datetime.now(BEIJING).isoformat(), "business_day": str(target_day),
                  "timezone": "Asia/Shanghai", "schedule": "02:00", "status": "running", "steps": {}}
        state["last_run"] = result
        _save_state(state_path, state)
        try:
            init_db()
        except Exception as exc:
            result.update(status="partial_failure", finished_at=datetime.now(BEIJING).isoformat(),
                          errors=[format_database_error(exc)])
            _save_state(state_path, state)
            return result
        jobs = {
            "deal": (run_fetch_deal_daily, get_unanalyzed_deal_customers, analyze_unlearned_deals),
            "delete": (run_fetch_delete_daily, get_unanalyzed_delete_customers, analyze_unlearned_deletes),
        }
        errors = []
        # 导入独立于AI配置；AI暂时不可用时仍将聊天安全入库。
        for kind, (fetch, _, _) in jobs.items():
            step = result["steps"][kind] = {"imports": [], "learning": {}}
            for day in days_to_import(imported.get(kind), target_day):
                try:
                    fetched = fetch(target_day=datetime.combine(day, datetime.min.time()))
                    summary = fetched.get("summary") or {}
                    settlement = summary.get("settlement") or {}
                    import_errors = summary.get("errors") or []
                    failed = bool(import_errors or settlement.get("failed_count") or summary.get("conflicts"))
                    ok = bool(fetched.get("ok")) and not failed
                    item = {"day": str(day), "ok": ok,
                            "customers_created": summary.get("customers_created", 0),
                            "messages_inserted": summary.get("messages_inserted", 0)}
                    if not ok:
                        item["error"] = core.redact_secrets(fetched.get("error") or "部分会话导入失败或冲突，请查看导入日志")
                    step["imports"].append(item)
                    if not ok:
                        errors.append(f"{kind} {day} 导入失败")
                        break
                    imported[kind] = str(day)
                    _save_state(state_path, state)
                except Exception as exc:
                    errors.append(f"{kind} {day}: {core.redact_secrets(str(exc))}")
                    break
            _save_state(state_path, state)
        try:
            cfg, warning = core.load_config_from_disk()
        except Exception:
            cfg, warning = {}, "AI配置不可用"
        if warning or not cfg.get("api_key") or cfg.get("api_key") == "YOUR_API_KEY_HERE":
            errors.append("AI配置不可用，已导入的客户保留待分析")
        else:
            for kind, (_, pending, learn) in jobs.items():
                try:
                    # 快照只处理一遍，失败留到下次；新客户全部处理，历史继承每类每晚最多50位。
                    customers, repairs = select_learning_customers(pending(limit=1000000), repair_limit)
                    learned = learn(cfg, customers=customers)
                    result["steps"][kind]["learning"] = {
                        key: learned.get(key, 0) for key in ("total", "success", "failed", "full", "incremental")
                    }
                    result["steps"][kind]["learning"]["repair_remaining"] = sum(bool(c.get("needs_repair")) for c in pending(limit=1000000))
                    if learned.get("failed"):
                        errors.append(f"{kind} {learned['failed']} 位客户分析失败，等待下次重试")
                except Exception as exc:
                    errors.append(f"{kind} 分析失败: {core.redact_secrets(str(exc), cfg)}")
                _save_state(state_path, state)
        result.update(status="partial_failure" if errors else "success",
                      finished_at=datetime.now(BEIJING).isoformat(), errors=errors)
        _save_state(state_path, state)
        logger.info("NIGHTLY_RESULT %s", json.dumps(result, ensure_ascii=False))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="", help="指定业务日YYYY-MM-DD，默认北京时间昨天")
    parser.add_argument("--repair-limit", type=int, default=50, help="每类历史继承分析每晚补跑上限")
    parser.add_argument("--check", action="store_true", help="只检查配置和数据库连接，不导入、不调用AI")
    args = parser.parse_args()
    import qc_core as core
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if args.check:
        from db import test_mysql_connection
        from salesmartly_client import load_config
        test_mysql_connection()
        load_config()
        cfg, warning = core.load_config_from_disk()
        if warning or not cfg.get("api_key") or cfg.get("api_key") == "YOUR_API_KEY_HERE":
            raise RuntimeError("AI配置不可用")
        print("NIGHTLY_CHECK_OK timezone=Asia/Shanghai schedule=02:00")
        return 0
    try:
        result = run_pipeline(target_day=date.fromisoformat(args.date) if args.date else None,
                              repair_limit=args.repair_limit)
        return 0 if result["status"] == "success" else 1
    except BlockingIOError:
        print("NIGHTLY_ALREADY_RUNNING")
        return 0
    except Exception as exc:
        logger.error("NIGHTLY_FAILED %s", core.redact_secrets(str(exc)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
