# -*- coding: utf-8 -*-
"""飞书群机器人 Webhook 通知。"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_APP_DIR, ".env")

# 飞书单条文本建议不超过约 4000 字
_MAX_MESSAGE_CHARS = 3800
_DEFAULT_LOG_TAIL_LINES = 30


def _load_env() -> None:
    if os.path.isfile(_ENV_PATH):
        load_dotenv(_ENV_PATH)
    else:
        load_dotenv()


def get_feishu_webhook_url() -> str:
    _load_env()
    return (os.getenv("FEISHU_WEBHOOK_URL") or "").strip()


def send_feishu_text(text: str, webhook_url: str | None = None) -> bool:
    """发送飞书文本消息；失败只记日志，不抛异常。"""
    url = (webhook_url or get_feishu_webhook_url()).strip()
    if not url:
        logger.info("未配置 FEISHU_WEBHOOK_URL，跳过飞书通知")
        return False

    body = (text or "").strip()
    if not body:
        logger.warning("飞书消息为空，跳过发送")
        return False

    if len(body) > _MAX_MESSAGE_CHARS:
        body = body[: _MAX_MESSAGE_CHARS - 20] + "\n…（内容已截断）"

    payload = {
        "msg_type": "text",
        "content": {"text": body},
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            logger.warning("飞书通知 HTTP %s: %s", resp.status_code, resp.text[:300])
            return False
        data = resp.json()
        code = data.get("StatusCode", data.get("code"))
        if code not in (0, None) and data.get("msg") not in ("success", "ok", None):
            logger.warning("飞书通知返回异常: %s", data)
            return False
        logger.info("飞书通知已发送")
        return True
    except Exception as e:
        logger.warning("飞书通知发送失败: %s", e)
        return False


def _read_log_tail(log_path: str, lines: int = _DEFAULT_LOG_TAIL_LINES) -> str:
    if not log_path or not os.path.isfile(log_path):
        return ""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-lines:]
        return "".join(tail).strip()
    except Exception as e:
        logger.warning("读取日志失败 %s: %s", log_path, e)
        return ""


def build_daily_job_report(
    result: dict,
    log_path: str | None = None,
    log_tail_lines: int = _DEFAULT_LOG_TAIL_LINES,
) -> str:
    """组装每日成交学习日报文本。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = int(result.get("total") or 0)
    success = int(result.get("success") or 0)
    failed = int(result.get("failed") or 0)
    errors = result.get("errors") or []
    learned = result.get("learned_items") or []

    lines = [
        "【成交客户每日学习报告】",
        f"时间：{now}",
        f"待分析：{total}",
        f"成功：{success}",
        f"失败：{failed}",
    ]

    if learned:
        lines.append("")
        lines.append("今日新增智能体判断规则：")
        for item in learned[:8]:
            name = item.get("contact_name") or "?"
            rule = (item.get("recommended_agent_rules") or "").strip()
            obj = (item.get("main_objection") or "").strip()
            head = f"· {name}"
            if obj:
                head += f"（{obj[:40]}）"
            lines.append(head)
            if rule:
                lines.append(f"  {rule[:280]}")
        if len(learned) > 8:
            lines.append(f"… 另有 {len(learned) - 8} 条未展示")

    if errors:
        lines.append("")
        lines.append("失败详情：")
        for err in errors[:10]:
            lines.append(f"· {err}")
        if len(errors) > 10:
            lines.append(f"… 另有 {len(errors) - 10} 条未展示")

    if log_path:
        tail = _read_log_tail(log_path, log_tail_lines)
        if tail:
            lines.append("")
            lines.append(f"--- 最近日志（{log_tail_lines} 行）---")
            lines.append(tail)

    return "\n".join(lines)


def notify_daily_job_result(
    result: dict,
    log_path: str | None = None,
    log_tail_lines: int = _DEFAULT_LOG_TAIL_LINES,
) -> bool:
    """分析完成后推送飞书日报。"""
    msg = build_daily_job_report(result, log_path=log_path, log_tail_lines=log_tail_lines)
    return send_feishu_text(msg)
