# -*- coding: utf-8 -*-
"""飞书群机器人 Webhook 通知。"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

import qc_core as core

logger = logging.getLogger(__name__)

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(core.get_config_dir(), ".env")

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


def _default_business_day(when: datetime | None = None) -> datetime:
    """每日学习对应的业务日 = 发送日的前一天（与 fetch_deal_daily 一致）。"""
    base = (when or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
    return base - timedelta(days=1)


def _format_business_day_label(result: dict) -> str:
    """如 7.12（2026-07-12）。"""
    day_str = (result.get("business_day") or "").strip()
    tag = (result.get("business_date_tag") or "").strip()
    if day_str and tag:
        return f"{tag.replace('..', '.')}（{day_str}）"
    if day_str:
        try:
            d = datetime.strptime(day_str, "%Y-%m-%d")
            return f"{d.month}.{d.day}（{day_str}）"
        except ValueError:
            return day_str
    biz = _default_business_day()
    return f"{biz.month}.{biz.day}（{biz.strftime('%Y-%m-%d')}）"


def build_daily_job_report(result: dict) -> str:
    """组装每日成交学习日报（精简版，不含日志尾部）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = int(result.get("total") or 0)
    success = int(result.get("success") or 0)
    failed = int(result.get("failed") or 0)
    kb_pending = result.get("kb_pending")

    lines = [
        "【成交客户每日学习报告】",
        f"时间：{now}",
        f"业务日：{_format_business_day_label(result)} 成交客户",
        f"待分析：{total}",
        f"成功：{success}",
        f"失败：{failed}",
    ]

    if kb_pending is not None:
        try:
            pending = int(kb_pending)
        except (TypeError, ValueError):
            pending = 0
        lines.append(f"未写入知识库：{pending} 条（待审核合并至 deal_learned_supplement.md）")

    if failed > 0 and result.get("errors"):
        lines.append("")
        lines.append("失败详情：")
        for err in (result.get("errors") or [])[:5]:
            lines.append(f"· {err}")

    return "\n".join(lines)


def notify_daily_job_result(result: dict) -> bool:
    """分析完成后推送飞书日报。"""
    msg = build_daily_job_report(result)
    return send_feishu_text(msg)


def build_fetch_deal_daily_report(
    result: dict,
    *,
    failed: bool = False,
    log_path: str | None = None,
    log_tail_lines: int = _DEFAULT_LOG_TAIL_LINES,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    meta = result.get("meta") or {}
    summary = result.get("summary") or {}
    settlement = summary.get("settlement") or {}

    lines = [
        "【成交客户每日拉取报告】",
        f"时间：{now}",
        f"状态：{'失败' if failed else '成功'}",
        f"业务日：{result.get('target_day', '')}",
        f"数据库：{result.get('target', '')}",
        f"日期标签：{meta.get('date_tag', '')}",
        f"标签下客户：{meta.get('contacts_total', 0)}",
        f"成交客户：{meta.get('deal_contacts', 0)}",
        f"有效会话：{meta.get('sessions_with_messages', meta.get('sessions', 0))}",
    ]

    if not failed and not result.get("dry_run"):
        lines.extend([
            f"成功入库：{settlement.get('success_count', summary.get('session_count', 0))}",
            f"标记已成交：{summary.get('marked_deal', 0)}",
            f"新增消息：{summary.get('messages_inserted', 0)}",
        ])

    err = (result.get("error") or "").strip()
    if err:
        lines.append("")
        lines.append(f"错误：{err}")

    if log_path:
        tail = _read_log_tail(log_path, log_tail_lines)
        if tail:
            lines.append("")
            lines.append(f"--- 最近日志（{log_tail_lines} 行）---")
            lines.append(tail)

    return "\n".join(lines)


def notify_fetch_deal_daily_result(
    result: dict,
    *,
    failed: bool = False,
    log_path: str | None = None,
    log_tail_lines: int = _DEFAULT_LOG_TAIL_LINES,
) -> bool:
    msg = build_fetch_deal_daily_report(
        result, failed=failed, log_path=log_path, log_tail_lines=log_tail_lines
    )
    return send_feishu_text(msg)
