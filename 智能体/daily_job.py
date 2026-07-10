# -*- coding: utf-8 -*-
"""
每日成交客户心理学习定时任务。

建议在每日拉取入库之后运行（fetch_deal_daily.py 建议 02:00，本任务 03:00）。

用法：
    python daily_job.py --limit 20
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
        logging.FileHandler(os.path.join(_LOG_DIR, "daily_job.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="每日成交客户心理学习")
    parser.add_argument("--limit", type=int, default=20, help="本次最多分析客户数")
    args = parser.parse_args()

    import qc_core as core
    from db import init_db
    from deal_intelligence import analyze_unlearned_deals

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

    result = analyze_unlearned_deals(cfg, limit=args.limit)

    print("今日成交学习完成")
    print(f"新增待分析成交客户：{result['total']}")
    print(f"成功分析：{result['success']}")
    print(f"失败：{result['failed']}")

    if result["errors"]:
        for err in result["errors"]:
            logger.warning("分析失败：%s", err)

    from feishu_notify import notify_daily_job_result

    log_path = os.path.join(_LOG_DIR, "daily_job.log")
    if notify_daily_job_result(result, log_path=log_path):
        print("飞书通知已发送")
    else:
        print("飞书通知未发送（未配置 Webhook 或发送失败，详见日志）")


if __name__ == "__main__":
    main()
