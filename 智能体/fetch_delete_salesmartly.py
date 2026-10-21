# -*- coding: utf-8 -*-
"""先按删除标签筛客户，再按自选沟通时间及客户有效发言数筛选；不调用 AI。"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

import qc_core as core
from fetch_cancel import raise_if_cancelled
from fetch_deal_salesmartly import (
    CHANNEL_MAP, _fetch_messages_for_sessions, _fetch_sessions_by_ids,
    _format_labels, _format_message_line, _should_include_in_transcript,
    build_blacklist_pattern, build_delete_pattern, build_member_maps, fetch_members,
)

logger = logging.getLogger(__name__)
DELETE_COLUMNS = ["联系人", "会话ID", "访客标签", "接待成员", "社媒渠道", "客户发言次数", "范围内客户发言次数", "会话消息内容"]


def fetch_delete_dataframe(
    client, *, window_start: datetime, window_end: datetime,
    delete_keywords=None, blacklist_keywords=None, on_progress=None, cancel_check=None,
):
    """窗口为 [start, end)；窗口内有客户有效沟通，完整历史客户发言 >6；返回完整聊天供流失复盘。"""
    if window_start >= window_end:
        raise ValueError("结束时间必须晚于开始时间。")
    delete_pattern = build_delete_pattern(delete_keywords)
    blacklist_pattern = build_blacklist_pattern(blacklist_keywords)

    def report(msg):
        raise_if_cancelled(cancel_check)
        logger.info(msg)
        if on_progress:
            on_progress(msg)

    report("拉取访客标签，查找删除标签…")
    labels, _ = client.get_all_pages(
        "/api/v2/get-label-list", page_size=200, max_pages=500, cancel_check=cancel_check,
    )
    raise_if_cancelled(cancel_check)
    id_to_name = {
        str(label["id"]): str(label.get("label_name") or "").strip()
        for label in labels if label.get("id") is not None
    }
    delete_labels = {
        lid: name for lid, name in id_to_name.items()
        if delete_pattern.search(name) and not blacklist_pattern.search(name)
    }
    if not delete_labels:
        raise ValueError("未找到匹配的删除标签，请核对 SaleSmartly 访客标签及删除标签关键词配置。")

    # 不能按最后回复时间或客户更新时间预筛：最后回复在范围外的客户，范围内仍可能有沟通。
    contacts = {}
    for idx, (label_id, label_name) in enumerate(delete_labels.items(), 1):
        report(f"拉取删除标签客户 {idx}/{len(delete_labels)}：{label_name}")
        items, _ = client.get_all_pages(
            "/api/v2/get-contact-list", {"labels": label_id},
            page_size=100, max_pages=500, cancel_check=cancel_check,
        )
        raise_if_cancelled(cancel_check)
        for contact in items:
            uid = str(contact.get("chat_user_id") or "").strip()
            if not uid:
                continue
            resolved = _format_labels(contact.get("labels"), id_to_name)
            previous = contacts.get(uid, {}).get("_labels_str", "")
            contacts[uid] = dict(contact, _labels_str=",".join(filter(None, [previous, resolved, label_name])))

    deleted = {
        uid: contact for uid, contact in contacts.items()
        if not blacklist_pattern.search(contact["_labels_str"])
    }
    meta = {
        "window_start": window_start.strftime("%Y-%m-%d %H:%M:%S"),
        "window_end": window_end.strftime("%Y-%m-%d %H:%M:%S"),
        "delete_labels": list(delete_labels.values()), "contacts_total": len(contacts),
        "delete_contacts": len(deleted), "excluded_blacklist": len(contacts) - len(deleted),
        "sessions": 0, "sessions_with_messages": 0, "skipped_no_customer_speech": 0,
        "skipped_insufficient_speech": 0, "skipped_no_session": 0,
    }
    report(f"删除标签下 {len(contacts)} 人，排除黑粉 {meta['excluded_blacklist']} 人")
    if not deleted:
        return pd.DataFrame(columns=DELETE_COLUMNS), meta

    session_ids = list(dict.fromkeys(
        str(c.get("session_id") or "").strip() for c in deleted.values() if c.get("session_id")
    ))
    meta["sessions"] = len(session_ids)
    meta["skipped_no_session"] = sum(not c.get("session_id") for c in deleted.values())
    if not session_ids:
        return pd.DataFrame(columns=DELETE_COLUMNS), meta
    report(f"拉取 {len(session_ids)} 个删除客户会话详情…")
    sessions = _fetch_sessions_by_ids(
        client.config, session_ids,
        on_progress=lambda done, total: report(f"会话详情 {done}/{total}"), cancel_check=cancel_check,
    )
    by_id = {str(s.get("session_id") or ""): s for s in sessions}
    meta["session_details_missing"] = sum(sid not in by_id for sid in session_ids)
    report("拉取删除客户完整聊天记录…")
    messages = _fetch_messages_for_sessions(
        client.config, session_ids,
        on_progress=lambda done, total, count: report(f"聊天记录 {done}/{total}，共 {count} 条"),
        cancel_check=cancel_check,
    )
    raise_if_cancelled(cancel_check)
    members = build_member_maps(fetch_members(client))
    rows = []
    seen_sessions = set()
    for uid, contact in deleted.items():
        raise_if_cancelled(cancel_check)
        sid = str(contact.get("session_id") or "").strip()
        if not sid or sid in seen_sessions:
            continue
        seen_sessions.add(sid)
        session = by_id.get(sid, {})
        name = str(contact.get("name") or session.get("title") or uid).strip()
        dialog = "\n".join(
            _format_message_line(m, members) for m in messages.get(sid, [])
            if _should_include_in_transcript(m)
        )
        dialog = core.normalize_customer_speaker_in_dialog(dialog, name)
        scoped = core.filter_dialog_by_window(dialog, window_start, window_end)
        window_count, _ = core.count_roles(scoped)
        if not window_count:
            meta["skipped_no_customer_speech"] += 1
            continue
        customer_count, _ = core.count_roles(dialog)
        if customer_count <= 6:
            meta["skipped_insufficient_speech"] += 1
            continue
        agent_id = session.get("sys_user_id") or contact.get("sys_user_id")
        try:
            agent_name = members.get(int(agent_id), str(agent_id or ""))
        except (TypeError, ValueError):
            agent_name = ""
        channel = session.get("channel") or contact.get("channel")
        rows.append({
            "联系人": name, "会话ID": sid, "访客标签": contact["_labels_str"],
            "接待成员": agent_name, "社媒渠道": CHANNEL_MAP.get(channel, str(channel or "")),
            "客户发言次数": customer_count, "范围内客户发言次数": window_count, "会话消息内容": dialog,
        })
    meta["sessions_with_messages"] = len(rows)
    report(
        f"完成：保留 {len(rows)} 通；范围内无客户有效沟通 {meta['skipped_no_customer_speech']} 通，"
        f"完整历史客户发言不足 7 条 {meta['skipped_insufficient_speech']} 通"
    )
    return pd.DataFrame(rows, columns=DELETE_COLUMNS), meta
