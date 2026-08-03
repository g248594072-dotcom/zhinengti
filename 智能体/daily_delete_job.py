# -*- coding: utf-8 -*-
"""
每日删除客户流失复盘定时任务。

建议在每日拉取入库之后运行（fetch_delete_daily.py 建议 02:15，本任务 03:30）。

用法：
    python daily_delete_job.py --limit 20
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

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
        logging.FileHandler(os.path.join(_LOG_DIR, "daily_delete_job.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="每日删除客户流失复盘")
    parser.add_argument("--limit", type=int, default=20, help="本次最多分析客户数")
    args = parser.parse_args()

    import qc_core as core
    from db import init_db
    from delete_intelligence import analyze_unlearned_deletes

    try:
        init_db()
        logger.info("数据库初始化完成")
    except Exception as e:
        logger.error("数据库初始化失败：%s", e)
        print(f"数据库初始化失败：{e}")
        sys.exit(1)

    cfg, warn = core.load_config_from_disk()
    if warn:
        logger.error("配置加载失败：%s", warn)
        print(f"配置错误：{warn}")
        sys.exit(1)

    if not cfg.get("api_key") or str(cfg["api_key"]).strip() in ("", "YOUR_API_KEY_HERE"):
        msg = "未配置有效 API Key，请在 qc_config.json 中填写 api_key"
        logger.error(msg)
        print(msg)
        sys.exit(1)

    result = analyze_unlearned_deletes(cfg, limit=args.limit)

    from fetch_deal_salesmartly import date_tag_name, yesterday_for_run
    from weekly_prompt_merge import count_delete_rules_not_in_supplement

    business_day = yesterday_for_run()
    kb_stats = count_delete_rules_not_in_supplement()
    result["business_day"] = business_day.strftime("%Y-%m-%d")
    result["business_date_tag"] = date_tag_name(business_day)
    result["kb_pending"] = kb_stats.get("pending", 0)

    print("今日删除客户流失复盘完成")
    print(f"业务日：{result['business_day']}（标签 {result['business_date_tag']}）")
    print(f"新增待分析删除客户：{result['total']}")
    print(f"成功分析：{result['success']}")
    print(f"失败：{result['failed']}")
    print(f"未写入知识库：{result['kb_pending']} 条")

    churn_stats = result.get("churn_stats") or {}
    if churn_stats:
        print("流失模式：" + " / ".join(f"{k} {v}" for k, v in churn_stats.items()))

    if result["errors"]:
        for err in result["errors"]:
            logger.warning("分析失败：%s", err)

    from feishu_notify import notify_daily_delete_job_result

    if notify_daily_delete_job_result(result):
        print("飞书通知已发送")
    else:
        print("飞书通知未发送（未配置 Webhook 或发送失败，详见日志）")


if __name__ == "__main__":
    main()
