# -*- coding: utf-8 -*-
"""从 SaleSmartly API 拉取昨日成交客户并拼成可导入的 DataFrame。"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Set

import pandas as pd

from salesmartly_client import (
    Config,
    DEFAULT_MAX_WORKERS,
    DEFAULT_PAGE_SIZE,
    SaleSmartlyClient,
)

logger = logging.getLogger(__name__)

CHANNEL_MAP = {
    1: "Messenger",
    2: "聊天插件",
    3: "Email",
    4: "Telegram Bot",
    5: "Instagram",
    6: "Line",
    7: "WhatsApp Api",
    8: "Facebook主页评论",
    10: "Slack",
    11: "微信客服",
    12: "whatsapp App",
    13: "instagram评论",
    15: "telegram App",
    16: "Tiktok App",
    17: "Tiktok评论",
    18: "Vkontakte",
    19: "zalo App",
    20: "tiktok Business",
    21: "tiktok Business 评论",
}

DEFAULT_DEAL_KEYWORDS = ("全款", "定金", "分期")


def yesterday_for_run(when: datetime | None = None) -> datetime:
    """默认「昨天」= 当前日期的前一天（用于日期标签 7..9 形式）。"""
    base = (when or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
    return base - timedelta(days=1)


def date_tag_name(day: datetime) -> str:
    return f"{day.month}..{day.day}"


def build_deal_pattern(keywords: List[str] | None = None) -> re.Pattern:
    words = [w.strip() for w in (keywords or DEFAULT_DEAL_KEYWORDS) if w and str(w).strip()]
    if not words:
        words = list(DEFAULT_DEAL_KEYWORDS)
    return re.compile("|".join(re.escape(w) for w in words))


def is_deal_contact(contact: dict, pattern: re.Pattern | None = None) -> bool:
    labels = _format_labels(contact.get("labels"))
    return bool((pattern or build_deal_pattern()).search(labels))


def build_tag_value_id_map(
    client: SaleSmartlyClient,
    category_keyword: str = "澳大利亚",
    label_keyword: str = "日期",
) -> Dict[str, str]:
    """时间标签名 -> 标签值 ID，如 {'7..9': '6360150'}。"""
    cat_data = client.get("/api/v2/visitor-label/categories", {})
    categories = cat_data.get("list") or []

    category_id = None
    for cat in categories:
        if category_keyword in str(cat.get("category_name", "")):
            category_id = cat.get("id")
            break
    if category_id is None:
        return {}

    target = None
    for page in range(1, 30):
        data = client.get(
            "/api/v2/visitor-label/labels",
            {
                "page": str(page),
                "page_size": "200",
                "category_id": str(category_id),
                "include_values": "1",
            },
        )
        items = data.get("list") or []
        for lab in items:
            if label_keyword in str(lab.get("label_name", "")):
                target = lab
                break
        if target is not None:
            break
        if len(items) < 200:
            break

    if target is None:
        return {}

    out: Dict[str, str] = {}
    for item in target.get("values") or []:
        name = str(item.get("value_name", "")).strip()
        vid = item.get("id")
        if name and vid is not None:
            out[name] = str(vid)
    return out


def fetch_members(client: SaleSmartlyClient) -> List[dict]:
    items, _ = client.get_all_pages("/api/v2/get-member-list", page_size=100, max_pages=20)
    return items


def member_display_name(member: dict) -> str:
    for key in ("nickname", "member_name", "name", "sys_user_name"):
        val = member.get(key)
        if val:
            return str(val).strip()
    return str(member.get("sys_user_id", "未知"))


def build_member_maps(members: List[dict]) -> Dict[int, str]:
    id_to_name: Dict[int, str] = {}
    for m in members:
        uid = m.get("sys_user_id")
        if uid is None:
            continue
        try:
            uid_int = int(uid)
        except (TypeError, ValueError):
            continue
        id_to_name[uid_int] = member_display_name(m)
    return id_to_name


PRIORITY_GROUP_ORDER = ("一转高", "一转西")


def build_member_groups(members: List[dict]) -> Dict[str, List[tuple[int, str]]]:
    """按 SaleSmartly 分组整理客服；同一 sys_user_id 只归入一个展示分组（优先 一转高/一转西）。"""
    uid_info: Dict[int, tuple[str, List[str]]] = {}
    for m in members:
        uid = m.get("sys_user_id")
        if uid is None:
            continue
        try:
            uid_int = int(uid)
        except (TypeError, ValueError):
            continue
        name = member_display_name(m)
        raw_groups = m.get("groups") or []
        group_names = [
            str(g.get("group_name") or "未分组").strip() or "未分组"
            for g in raw_groups
        ]
        uid_info[uid_int] = (name, group_names)

    def _pick_group(group_names: List[str]) -> str:
        for prio in PRIORITY_GROUP_ORDER:
            if prio in group_names:
                return prio
        if group_names:
            return group_names[0]
        return "未分组"

    groups: Dict[str, List[tuple[int, str]]] = {}
    for uid_int, (name, group_names) in uid_info.items():
        gname = _pick_group(group_names)
        groups.setdefault(gname, []).append((uid_int, name))

    for gname in groups:
        groups[gname].sort(key=lambda x: x[1])
    return groups


def sort_member_group_names(group_names) -> List[str]:
    def _key(name: str) -> tuple:
        try:
            return (0, PRIORITY_GROUP_ORDER.index(name))
        except ValueError:
            return (1, name)

    return sorted(group_names, key=_key)


def _format_labels(raw, id_to_name: Optional[Dict[str, str]] = None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict):
                val = item.get("label_name") or item.get("name") or item.get("value")
                if val is not None:
                    parts.append(str(val))
            else:
                parts.append(str(item))
        return ",".join(parts)
    s = str(raw).strip()
    if not s:
        return ""
    if s.startswith("[") or s.startswith("{"):
        try:
            data = json.loads(s)
            return _format_labels(data, id_to_name)
        except Exception:
            return s
    if id_to_name and re.fullmatch(r"[\d,\s]+", s):
        parts = []
        for token in s.split(","):
            token = token.strip()
            if not token:
                continue
            parts.append(id_to_name.get(token, token))
        return ",".join(parts)
    return s


def _format_ts(ts) -> str:
    if not ts:
        return ""
    try:
        val = int(ts)
    except (TypeError, ValueError):
        return ""
    if val > 1_000_000_000_000:
        val = val // 1000
    return datetime.fromtimestamp(val).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_display_text(text: str) -> str:
    t = (text or "").strip()
    if t in ("[图片]", "[图片消息]"):
        return "图片"
    return t


def _is_system_message(msg: dict) -> bool:
    if msg.get("msg_type") == 8:
        return True
    text = str(msg.get("text") or "").strip()
    if text in ("[系统消息]", "系统消息"):
        return True
    content = msg.get("content")
    if isinstance(content, str) and content.lstrip().startswith("{"):
        try:
            obj = json.loads(content)
            inner = obj.get("msg")
            if isinstance(inner, dict):
                msg_type = str(inner.get("type") or "").strip()
                if msg_type in (
                    "messenger_referral",
                    "create_session",
                    "assign_session",
                    "close_session",
                ):
                    return True
        except Exception:
            pass
    return False


def _message_text(msg: dict) -> str:
    text = msg.get("text")
    if text is not None and str(text).strip() != "":
        return _normalize_display_text(str(text).strip())
    content = msg.get("content")
    if not content:
        return ""
    if isinstance(content, str) and content.lstrip().startswith("{"):
        try:
            obj = json.loads(content)
            inner = obj.get("msg")
            if isinstance(inner, dict):
                if inner.get("file_type") == "image" or inner.get("file_url"):
                    return "图片"
                return str(inner.get("caption") or inner.get("file_name") or "").strip()
            if inner is not None:
                return str(inner).strip()
            res_msg = (obj.get("res") or {}).get("message") or {}
            return str(res_msg.get("text") or "").strip()
        except Exception:
            return _normalize_display_text(str(content).strip())
    return _normalize_display_text(str(content).strip())


def _should_include_in_transcript(msg: dict) -> bool:
    if _is_system_message(msg):
        return False
    return bool(_message_text(msg)) or msg.get("msg_type") == 2


def _format_message_line(msg: dict, member_id_to_name: Dict[int, str]) -> str:
    msg_type = msg.get("msg_type", 1)
    text = _message_text(msg)
    if msg_type == 2 and not text:
        text = "图片"

    sender_type = msg.get("sender_type", 0)
    if sender_type == 1:
        role = "客户"
    elif sender_type == 2:
        sender = str(msg.get("sender") or "").strip()
        try:
            role = member_id_to_name.get(int(sender), sender) or "客服"
        except (TypeError, ValueError):
            role = sender or "客服"
    else:
        role = str(msg.get("sender") or "客服").strip() or "客服"

    ts = _format_ts(msg.get("send_time"))
    return f'[{ts}] {role} : "{text}"'


def _fetch_contacts_by_tag_value(
    config: Config,
    tag_value_id: str,
) -> Dict[str, dict]:
    contacts_by_uid: Dict[str, dict] = {}
    items, _ = SaleSmartlyClient(config).get_all_pages(
        "/api/v2/get-contact-list",
        {"labels": str(tag_value_id)},
        page_size=100,
        max_pages=500,
    )
    for contact in items:
        uid = str(contact.get("chat_user_id") or "")
        if uid:
            contacts_by_uid[uid] = contact
    return contacts_by_uid


def _fetch_sessions_by_ids(
    config: Config,
    session_ids: List[str],
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> List[dict]:
    if not session_ids:
        return []

    sessions: List[dict] = []
    total = len(session_ids)
    done = 0

    def _fetch_one(session_id: str) -> Optional[dict]:
        try:
            data = SaleSmartlyClient(config).get(
                "/api/v2/get-session-list",
                {
                    "session_id": session_id,
                    "session_status": "0",
                    "page": "1",
                    "page_size": "1",
                },
            )
            items = data.get("list") or []
            return items[0] if items else None
        except Exception:
            return None

    workers = min(DEFAULT_MAX_WORKERS, max(1, total))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, sid): sid for sid in session_ids}
        for fut in as_completed(futures):
            session = fut.result()
            if session:
                sessions.append(session)
            done += 1
            if on_progress:
                on_progress(done, total)
    return sessions


def _fetch_messages_for_sessions(
    config: Config,
    session_ids: List[str],
    on_progress: Optional[Callable[[int, int, int], None]] = None,
) -> Dict[str, List[dict]]:
    if not session_ids:
        return {}

    grouped: Dict[str, List[dict]] = {}

    def _fetch_one(session_id: str) -> tuple[str, List[dict]]:
        client = SaleSmartlyClient(config)
        items, _ = client.get_all_pages(
            "/api/v2/get-all-message-list",
            {"session_id": session_id},
            page_size=DEFAULT_PAGE_SIZE,
            max_pages=100,
        )
        items.sort(key=lambda m: m.get("send_time") or 0)
        return session_id, items

    workers = min(DEFAULT_MAX_WORKERS, max(1, len(session_ids)))
    done = 0
    msg_count = 0
    total = len(session_ids)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, sid): sid for sid in session_ids}
        for fut in as_completed(futures):
            sid, items = fut.result()
            if items:
                grouped[sid] = items
                msg_count += len(items)
            done += 1
            if on_progress:
                on_progress(done, total, msg_count)

    return grouped


def fetch_yesterday_deal_dataframe(
    client: SaleSmartlyClient,
    *,
    target_day: datetime | None = None,
    deal_keywords: List[str] | None = None,
    date_label_category: str = "澳大利亚",
    date_label_name: str = "日期",
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[pd.DataFrame, dict]:
    """
    拉取指定业务日（默认昨天）带成交标签的客户聊天记录。

    返回 (DataFrame, meta)，meta 含统计与日期标签信息。
    """
    day = target_day or yesterday_for_run()
    tag_name = date_tag_name(day)
    deal_pattern = build_deal_pattern(deal_keywords)
    config = client.config

    def _log(msg: str) -> None:
        logger.info(msg)
        if on_progress:
            on_progress(msg)

    _log(f"查找日期标签：{tag_name}")
    tag_map = build_tag_value_id_map(client, date_label_category, date_label_name)
    tag_value_id = tag_map.get(tag_name)
    if not tag_value_id:
        raise ValueError(
            f"未找到日期标签「{tag_name}」。"
            f"可用标签：{', '.join(sorted(tag_map.keys())[-15:]) or '（无）'}"
        )

    _log(f"拉取标签客户（ID={tag_value_id}）…")
    contacts_map = _fetch_contacts_by_tag_value(config, tag_value_id)
    id_to_name = {vid: name for name, vid in tag_map.items()}

    deal_contacts: Dict[str, dict] = {}
    for uid, contact in contacts_map.items():
        labels_str = _format_labels(contact.get("labels"), id_to_name)
        contact = dict(contact)
        contact["_labels_str"] = labels_str
        if deal_pattern.search(labels_str):
            deal_contacts[uid] = contact

    _log(f"日期标签下 {len(contacts_map)} 人，成交客户 {len(deal_contacts)} 人")
    if not deal_contacts:
        empty = pd.DataFrame(columns=[
            "联系人", "会话ID", "接待成员", "社媒渠道", "会话消息内容",
        ])
        return empty, {
            "date_tag": tag_name,
            "target_day": day.strftime("%Y-%m-%d"),
            "contacts_total": len(contacts_map),
            "deal_contacts": 0,
            "sessions": 0,
        }

    session_ids: List[str] = []
    seen_sids: Set[str] = set()
    for contact in deal_contacts.values():
        sid = str(contact.get("session_id") or "").strip()
        if sid and sid not in seen_sids:
            seen_sids.add(sid)
            session_ids.append(sid)

    _log(f"拉取 {len(session_ids)} 个会话…")

    def _session_progress(done: int, total: int) -> None:
        if on_progress:
            on_progress(f"会话详情 {done}/{total}")

    sessions = _fetch_sessions_by_ids(config, session_ids, on_progress=_session_progress)
    session_by_id = {str(s.get("session_id") or ""): s for s in sessions if s.get("session_id")}

    missing = [sid for sid in session_ids if sid not in session_by_id]
    if missing:
        logger.warning("有 %d 个会话未能从 API 获取详情", len(missing))

    _log("拉取完整聊天记录…")

    def _msg_progress(done: int, total: int, msg_count: int) -> None:
        if on_progress:
            on_progress(f"聊天记录 {done}/{total}，共 {msg_count} 条")

    messages_by_session = _fetch_messages_for_sessions(
        config, list(session_by_id.keys()), on_progress=_msg_progress
    )

    members = fetch_members(client)
    member_id_to_name = build_member_maps(members)

    rows = []
    for uid, contact in deal_contacts.items():
        session_id = str(contact.get("session_id") or "").strip()
        session = session_by_id.get(session_id, {})
        contact_name = (
            str(contact.get("name") or "").strip()
            or str(session.get("title") or "").strip()
            or uid
        )
        labels_str = contact.get("_labels_str") or _format_labels(contact.get("labels"), id_to_name)

        msgs = messages_by_session.get(session_id, [])
        message_text = "\n".join(
            _format_message_line(m, member_id_to_name)
            for m in msgs
            if _should_include_in_transcript(m)
        )
        if not message_text.strip():
            continue

        sys_user_id = session.get("sys_user_id")
        try:
            agent_name = member_id_to_name.get(int(sys_user_id), "")
        except (TypeError, ValueError):
            agent_name = ""
        if not agent_name and sys_user_id not in (None, 0, "0"):
            agent_name = str(sys_user_id)

        channel_name = CHANNEL_MAP.get(session.get("channel"), str(session.get("channel") or ""))

        rows.append({
            "联系人": contact_name,
            "会话ID": session_id,
            "访客标签": labels_str,
            "接待成员": agent_name,
            "社媒渠道": channel_name,
            "会话消息内容": message_text,
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df.columns = df.columns.str.strip()

    meta = {
        "date_tag": tag_name,
        "target_day": day.strftime("%Y-%m-%d"),
        "contacts_total": len(contacts_map),
        "deal_contacts": len(deal_contacts),
        "sessions_with_messages": len(rows),
        "sessions": len(session_ids),
    }
    _log(f"完成：{len(rows)} 通有效会话")
    return df, meta
