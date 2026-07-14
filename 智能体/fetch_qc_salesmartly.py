# -*- coding: utf-8 -*-
"""从 SaleSmartly API 拉取质检用聊天记录（客服筛选 + 最后沟通时间 + 客户发言二次筛选）。"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Set, Tuple

import pandas as pd

import qc_core as core
from fetch_deal_salesmartly import (
    CHANNEL_MAP,
    _fetch_messages_for_sessions,
    _fetch_sessions_by_ids,
    _format_message_line,
    _should_include_in_transcript,
    build_member_maps,
    fetch_members,
    member_display_name,
)
from salesmartly_client import Config, DEFAULT_MAX_WORKERS, SaleSmartlyClient

logger = logging.getLogger(__name__)

UPDATED_TIME_LOOKBACK_DAYS = 90


def _wide_updated_time_range() -> str:
    end = datetime.now()
    start = end - timedelta(days=UPDATED_TIME_LOOKBACK_DAYS)
    return json.dumps({"start": int(start.timestamp()), "end": int(end.timestamp())})


def _msg_last_send_time_range(window_start: datetime, window_end: datetime) -> str:
    """最后沟通时间筛选（秒级）。"""
    end_ts = int(window_end.timestamp())
    if window_end.microsecond == 0:
        end_ts = max(int(window_start.timestamp()), end_ts - 1)
    return json.dumps({"start": int(window_start.timestamp()), "end": end_ts})


def _fetch_contacts_for_agent(
    config: Config,
    agent_id: int,
    *,
    window: Optional[Tuple[datetime, datetime]] = None,
) -> Dict[str, dict]:
    params: Dict[str, str] = {
        "updated_time": _wide_updated_time_range(),
        "sys_user_id": str(agent_id),
    }
    if window:
        params["msg_last_send_time"] = _msg_last_send_time_range(window[0], window[1])

    items, _ = SaleSmartlyClient(config).get_all_pages(
        "/api/v2/get-contact-list",
        params,
        page_size=100,
        max_pages=200,
    )
    out: Dict[str, dict] = {}
    for contact in items:
        uid = str(contact.get("chat_user_id") or "")
        if uid:
            out[uid] = contact
    return out


def _fetch_contacts_for_agents(
    config: Config,
    agent_ids: List[int],
    *,
    window: Optional[Tuple[datetime, datetime]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, dict]:
    if not agent_ids:
        return {}

    merged: Dict[str, dict] = {}
    workers = min(DEFAULT_MAX_WORKERS, max(1, len(agent_ids)))

    def _log(msg: str) -> None:
        logger.info(msg)
        if on_progress:
            on_progress(msg)

    _log(f"并行拉取 {len(agent_ids)} 位客服的客户列表…")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_contacts_for_agent, config, aid, window=window): aid for aid in agent_ids}
        done = 0
        for fut in as_completed(futures):
            aid = futures[fut]
            try:
                part = fut.result()
                merged.update(part)
            except Exception as e:
                logger.warning("客服 %s 客户列表拉取失败：%s", aid, e)
            done += 1
            _log(f"客服客户列表 {done}/{len(agent_ids)}，累计 {len(merged)} 人")

    return merged


def _has_customer_speech_in_window(dialog: str, window: Optional[Tuple[datetime, datetime]]) -> bool:
    if not (dialog or "").strip():
        return False
    if window is None:
        cust, _ = core.count_roles(dialog)
        return cust > 0
    scoped = core.filter_dialog_by_window(dialog, window[0], window[1])
    if not scoped.strip():
        return False
    cust, _ = core.count_roles(scoped)
    return cust > 0


def fetch_qc_dataframe(
    client: SaleSmartlyClient,
    agent_ids: List[int],
    *,
    window: Optional[Tuple[datetime, datetime]] = None,
    require_customer_speech: bool = True,
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[pd.DataFrame, dict]:
    """
    拉取质检用 DataFrame。

    window: (start, end) 左闭右开；None 表示不按最后沟通时间筛、拉全量聊天。
    require_customer_speech: 窗口内客户发言数 > 0 才保留。
    """
    config = client.config
    agent_set = set(int(x) for x in agent_ids)

    def _log(msg: str) -> None:
        logger.info(msg)
        if on_progress:
            on_progress(msg)

    if not agent_set:
        raise ValueError("请至少选择 1 位接待客服")

    contacts_map = _fetch_contacts_for_agents(
        config, sorted(agent_set), window=window, on_progress=on_progress
    )
    _log(f"合并后 {len(contacts_map)} 位客户（最后沟通时间筛选已应用）")

    session_ids: List[str] = []
    seen_sids: Set[str] = set()
    for contact in contacts_map.values():
        sid = str(contact.get("session_id") or "").strip()
        if sid and sid not in seen_sids:
            seen_sids.add(sid)
            session_ids.append(sid)

    if not session_ids:
        empty = pd.DataFrame(columns=[
            "联系人", "会话ID", "接待成员", "社媒渠道", "会话消息内容",
        ])
        return empty, {
            "contacts_total": len(contacts_map),
            "sessions": 0,
            "sessions_kept": 0,
            "skipped_no_customer_speech": 0,
        }

    _log(f"并行拉取 {len(session_ids)} 个会话详情…")

    def _session_progress(done: int, total: int) -> None:
        _log(f"会话详情 {done}/{total}")

    sessions = _fetch_sessions_by_ids(config, session_ids, on_progress=_session_progress)
    session_by_id = {str(s.get("session_id") or ""): s for s in sessions if s.get("session_id")}

    def _msg_progress(done: int, total: int, msg_count: int) -> None:
        if on_progress:
            on_progress(f"聊天记录 {done}/{total}，共 {msg_count} 条")

    _log("并行拉取完整聊天记录…")
    messages_by_session = _fetch_messages_for_sessions(
        config, list(session_by_id.keys()), on_progress=_msg_progress
    )

    members = fetch_members(client)
    member_id_to_name = build_member_maps(members)

    rows = []
    skipped_no_speech = 0
    for uid, contact in contacts_map.items():
        session_id = str(contact.get("session_id") or "").strip()
        if not session_id:
            continue
        session = session_by_id.get(session_id, {})
        try:
            session_agent = int(session.get("sys_user_id") or 0)
        except (TypeError, ValueError):
            session_agent = 0
        if session_agent and session_agent not in agent_set:
            continue

        msgs = messages_by_session.get(session_id, [])
        contact_name = (
            str(contact.get("name") or "").strip()
            or str(session.get("title") or "").strip()
            or uid
        )
        message_text = "\n".join(
            _format_message_line(m, member_id_to_name)
            for m in msgs
            if _should_include_in_transcript(m)
        )
        if not message_text.strip():
            continue

        message_text = core.normalize_customer_speaker_in_dialog(message_text, contact_name)
        if not message_text.strip():
            continue

        if require_customer_speech and not _has_customer_speech_in_window(message_text, window):
            skipped_no_speech += 1
            continue

        sys_user_id = session.get("sys_user_id") or contact.get("sys_user_id")
        try:
            agent_name = member_id_to_name.get(int(sys_user_id), "")
        except (TypeError, ValueError):
            agent_name = ""
        if not agent_name and sys_user_id not in (None, 0, "0"):
            agent_name = str(sys_user_id)
        if not agent_name:
            agent_name = member_id_to_name.get(session_agent, "")

        channel_name = CHANNEL_MAP.get(session.get("channel"), str(session.get("channel") or ""))

        rows.append({
            "联系人": contact_name,
            "会话ID": session_id,
            "接待成员": agent_name,
            "社媒渠道": channel_name,
            "会话消息内容": message_text,
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df.columns = df.columns.str.strip()

    meta = {
        "contacts_total": len(contacts_map),
        "sessions": len(session_ids),
        "sessions_kept": len(rows),
        "skipped_no_customer_speech": skipped_no_speech,
        "agent_count": len(agent_set),
    }
    _log(f"完成：保留 {len(rows)} 通会话，跳过无客户发言 {skipped_no_speech}")
    return df, meta


def dataframe_to_sessions(
    df: pd.DataFrame,
    *,
    time_scope: str = core.TIME_SCOPE_TODAY,
    custom_window: Optional[Tuple[datetime, datetime]] = None,
) -> tuple[list[dict], Optional[dict]]:
    """DataFrame → 会话列表，并应用与 Excel 相同的时间窗口筛选。"""
    if df is None or df.empty:
        return [], None

    sessions, _, _ = core.load_sessions(
        file_dfs=[("SaleSmartly API", df)],
        time_scope=core.TIME_SCOPE_ALL,
    )
    if not sessions:
        return [], None

    window_info = None
    if time_scope == core.TIME_SCOPE_CUSTOM and custom_window:
        sessions, window_info = core.filter_sessions_by_window(
            sessions, custom_window[0], custom_window[1]
        )
    elif time_scope != core.TIME_SCOPE_ALL:
        sessions, window_info = core.apply_time_scope_to_sessions(sessions, time_scope)

    kept = []
    for s in sessions:
        cust, _ = core.count_roles(s.get("对话") or "")
        if cust > 0:
            kept.append(s)
    return kept, window_info
