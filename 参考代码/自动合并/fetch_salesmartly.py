# -*- coding: utf-8 -*-
"""从 SaleSmartly API 拉取数据并拼成与 Excel 导出等价的 DataFrame"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Set, Union

from fetch_progress import FetchCancelledError, FetchProgressReporter
from salesmartly_client import (
    Config,
    DEFAULT_MAX_WORKERS,
    DEFAULT_PAGE_SIZE,
    SaleSmartlyClient,
    app_dir,
)

# 筛选界面元数据缓存（缩短每次打开时的 API 等待）
FILTER_CACHE_TTL_SEC = 300


def _filter_cache_path() -> str:
    cache_dir = os.path.join(app_dir(), ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "filter_meta.json")


def load_filter_cache() -> Optional[dict]:
    path = _filter_cache_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - float(data.get("saved_at", 0)) > FILTER_CACHE_TTL_SEC:
            return None
        if int(data.get("month", 0)) != datetime.now().month:
            return None
        return data
    except Exception:
        return None


def save_filter_cache(members: List[dict], time_tags: List[str]) -> None:
    payload = {
        "saved_at": time.time(),
        "month": datetime.now().month,
        "members": members,
        "time_tags": time_tags,
    }
    with open(_filter_cache_path(), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def fetch_filter_metadata(client: SaleSmartlyClient) -> tuple[List[dict], List[str]]:
    """并行拉取客服列表与访客时间标签（筛选弹窗用）。"""
    cached = load_filter_cache()
    if cached is not None:
        return list(cached.get("members") or []), list(cached.get("time_tags") or [])

    with ThreadPoolExecutor(max_workers=2) as pool:
        members_future = pool.submit(fetch_members, client)
        tags_future = pool.submit(fetch_date_tag_values, client)
        members = members_future.result()
        all_time_tags = tags_future.result()

    time_tags = filter_current_month_time_tags(all_time_tags)
    save_filter_cache(members, time_tags)
    return members, time_tags

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

ProgressCallback = Union[FetchProgressReporter, None]


def _cancel_check(progress: ProgressCallback) -> Optional[Callable[[], bool]]:
    if progress is None:
        return None
    return progress.is_cancelled


def _check(progress: ProgressCallback) -> None:
    if progress is not None:
        progress.check_cancelled()


def _client(config: Config) -> SaleSmartlyClient:
    return SaleSmartlyClient(config)


# 业务日切点：标签日 = 前一日 20:00 至 当日 20:00（不含当日 20:00）
BUSINESS_DAY_CUTOFF_HOUR = 20


def business_day_window_for_date(tag_date: datetime) -> tuple[datetime, datetime]:
    """
    将标签日期映射为业务日时间窗。
    例如标签 7..10 → 7月9日 20:00:00 至 7月10日 19:59:59。
    """
    day = tag_date.replace(hour=0, minute=0, second=0, microsecond=0)
    start = (day - timedelta(days=1)).replace(
        hour=BUSINESS_DAY_CUTOFF_HOUR, minute=0, second=0, microsecond=0
    )
    end = day.replace(hour=BUSINESS_DAY_CUTOFF_HOUR, minute=0, second=0, microsecond=0) - timedelta(
        microseconds=1
    )
    return start, end


def business_window_from_tags(tag_names: List[str]) -> tuple[datetime, datetime]:
    """根据所选访客标签合并业务日时间窗（仅用于筛选会话，不用于截断聊天记录）。"""
    year = datetime.now().year
    if not tag_names:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return business_day_window_for_date(today)

    windows = []
    for tag in tag_names:
        tag_date = parse_time_tag(tag, year)
        if tag_date:
            windows.append(business_day_window_for_date(tag_date))
    if not windows:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return business_day_window_for_date(today)

    start = min(w[0] for w in windows)
    end = max(w[1] for w in windows)
    return start, end


def _datetime_range_ts(start_dt: datetime, end_dt: datetime) -> str:
    return json.dumps({"start": int(start_dt.timestamp()), "end": int(end_dt.timestamp())})


def _datetime_range_ms(start_dt: datetime, end_dt: datetime) -> str:
    return json.dumps(
        {
            "start": int(start_dt.timestamp() * 1000),
            "end": int(end_dt.timestamp() * 1000),
        }
    )


def _date_range_ts(start_date: str, end_date: str) -> str:
    """兼容旧接口：日历日 00:00–23:59（仅保留给未迁移调用）。"""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    return _datetime_range_ts(start, end)


def _date_range_ms(start_date: str, end_date: str) -> str:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, microsecond=999999
    )
    return _datetime_range_ms(start, end)


def parse_time_tag(tag: str, year: Optional[int] = None) -> Optional[datetime]:
    """将访客时间标签（如 7..10）解析为日期。"""
    m = re.fullmatch(r"(\d+)\.\.(\d+)", str(tag).strip())
    if not m:
        return None
    year = year or datetime.now().year
    try:
        return datetime(year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def date_range_from_tags(tag_names: List[str]) -> tuple[str, str]:
    """
    根据所选访客标签推算日历日期（用于展示）。
    实际 API 拉数时间窗见 business_window_from_tags（前一日 20:00 – 当日 20:00）。
    """
    year = datetime.now().year
    if not tag_names:
        now = datetime.now()
        start = datetime(now.year, now.month, 1)
        return start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")

    dates = [d for t in tag_names if (d := parse_time_tag(t, year))]
    if not dates:
        today = datetime.now().strftime("%Y-%m-%d")
        return today, today

    start = min(dates)
    end = max(dates)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

def fetch_members(client: SaleSmartlyClient) -> List[dict]:
    items, _ = client.get_all_pages("/api/v2/get-member-list", page_size=100, max_pages=20)
    return items


def _time_tag_sort_key(value: str) -> tuple:
    m = re.match(r"(\d+)\.\.(\d+)", str(value).strip())
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (0, 0)


def fetch_date_tag_values(
    client: SaleSmartlyClient,
    category_keyword: str = "澳大利亚",
    label_keyword: str = "日期",
) -> List[str]:
    """从访客标签 API 拉取「日期(澳)」类时间标签值，如 7..9、7..10。"""
    cat_data = client.get("/api/v2/visitor-label/categories", {})
    categories = cat_data.get("list") or []

    category_id = None
    for cat in categories:
        name = str(cat.get("category_name", ""))
        if category_keyword in name:
            category_id = cat.get("id")
            break
    if category_id is None:
        return []

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
        return []

    values: List[str] = []
    seen = set()
    for item in target.get("values") or []:
        name = str(item.get("value_name", "")).strip()
        if name and name not in seen:
            seen.add(name)
            values.append(name)

    values.sort(key=_time_tag_sort_key, reverse=True)
    return values


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

def filter_current_month_time_tags(tags: List[str]) -> List[str]:
    """只保留当月时间标签，如 7..10、7..9（与 SaleSmartly 界面一致）。"""
    month = datetime.now().month
    month_tags = [t for t in tags if re.fullmatch(rf"{month}\.\.\d+", str(t).strip())]
    if month_tags:
        month_tags.sort(key=_time_tag_sort_key, reverse=True)
        return month_tags
    return tags[:20]


def build_member_groups(members: List[dict]) -> Dict[str, List[tuple[int, str]]]:
    """按 SaleSmartly 分组整理客服：group_name -> [(sys_user_id, name), ...]"""
    groups: Dict[str, List[tuple[int, str]]] = {}
    for m in members:
        uid = m.get("sys_user_id")
        if uid is None:
            continue
        try:
            uid_int = int(uid)
        except (TypeError, ValueError):
            continue
        name = member_display_name(m)
        member_groups = m.get("groups") or []
        if not member_groups:
            groups.setdefault("未分组", []).append((uid_int, name))
            continue
        for g in member_groups:
            gname = str(g.get("group_name") or "未分组").strip() or "未分组"
            groups.setdefault(gname, []).append((uid_int, name))

    for gname in groups:
        groups[gname].sort(key=lambda x: x[1])
    return groups


PRIORITY_GROUP_ORDER = ("一转高", "一转西")


def sort_member_group_names(group_names: Iterable[str]) -> List[str]:
    """分组排序：一转高最上，一转西其次，其余按名称。"""
    def _key(name: str) -> tuple:
        try:
            return (0, PRIORITY_GROUP_ORDER.index(name))
        except ValueError:
            return (1, name)

    return sorted(group_names, key=_key)

def fetch_visitor_labels(client: SaleSmartlyClient) -> List[dict]:
    items, _ = client.get_all_pages(
        "/api/v2/get-label-list",
        page_size=200,
        max_pages=50,
    )
    seen = set()
    out = []
    for item in items:
        name = str(item.get("label_name", "")).strip()
        if name and name not in seen:
            seen.add(name)
            out.append(item)
    out.sort(key=lambda x: x.get("label_name", ""))
    return out


def member_display_name(member: dict) -> str:
    for key in ("nickname", "member_name", "name", "sys_user_name"):
        val = member.get(key)
        if val:
            return str(val).strip()
    return str(member.get("sys_user_id", "未知"))


def build_member_maps(members: List[dict]) -> tuple[Dict[int, str], Dict[str, int]]:
    id_to_name: Dict[int, str] = {}
    name_to_id: Dict[str, int] = {}
    for m in members:
        uid = m.get("sys_user_id")
        if uid is None:
            continue
        try:
            uid_int = int(uid)
        except (TypeError, ValueError):
            continue
        name = member_display_name(m)
        id_to_name[uid_int] = name
        name_to_id[name] = uid_int
    return id_to_name, name_to_id


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


def _selected_tag_value_ids(
    selected_tag_names: List[str],
    tag_value_id_map: Dict[str, str],
) -> List[str]:
    ids: List[str] = []
    seen: Set[str] = set()
    for name in selected_tag_names:
        vid = tag_value_id_map.get(str(name).strip())
        if vid and vid not in seen:
            seen.add(vid)
            ids.append(vid)
    return ids


def _session_label_ids(session: dict) -> Set[str]:
    raw = str(session.get("labels") or "")
    return {token.strip() for token in raw.split(",") if token.strip()}


def _session_matches_tag_ids(session: dict, tag_value_ids: Iterable[str]) -> bool:
    wanted = {str(x) for x in tag_value_ids if x}
    if not wanted:
        return True
    return bool(_session_label_ids(session) & wanted)


def _labels_for_row(
    session: dict,
    contact: dict,
    id_to_name: Dict[str, str],
) -> str:
    labels_str = _format_labels(contact.get("labels"), id_to_name)
    if labels_str.strip():
        return labels_str
    return _format_labels(session.get("labels"), id_to_name)

def _format_session_tags(raw) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    if s.startswith("["):
        try:
            data = json.loads(s)
            names = []
            for item in data:
                if isinstance(item, dict):
                    if item.get("tag_name"):
                        names.append(str(item["tag_name"]))
                    for child in item.get("children") or []:
                        if isinstance(child, dict) and child.get("tag_name"):
                            names.append(str(child["tag_name"]))
            return ",".join(names) if names else s
        except Exception:
            return s
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
    """与 SaleSmartly Excel 导出对齐的展示文本。"""
    t = (text or "").strip()
    if t in ("[图片]", "[图片消息]"):
        return "图片"
    return t


def _is_system_message(msg: dict) -> bool:
    """系统事件消息（广告来源、建会话等），Excel 导出不包含，统计时应跳过。"""
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


def _should_include_in_transcript(msg: dict) -> bool:
    if _is_system_message(msg):
        return False
    return bool(_message_text(msg)) or msg.get("msg_type") == 2


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


def _contact_matches_tags(labels_str: str, selected_tags: Iterable[str]) -> bool:
    selected = [t for t in selected_tags if t]
    if not selected:
        return True
    hay = str(labels_str or "")
    return any(tag in hay for tag in selected)


def _fetch_sessions_for_agents(
    config: Config,
    window_start: datetime,
    window_end: datetime,
    agent_ids: List[int],
    progress: ProgressCallback = None,
) -> List[dict]:
    time_range = _datetime_range_ts(window_start, window_end)
    cancel = _cancel_check(progress)
    total = len(agent_ids)
    if progress:
        progress.set_stage(
            "第 2/3 步：拉取客服会话",
            total,
            "关闭此窗口将终止拉取",
        )

    sessions: List[dict] = []
    seen_keys: Set[str] = set()

    def _fetch_one(agent_id: int) -> List[dict]:
        if cancel and cancel():
            return []
        client = _client(config)
        items, _ = client.get_all_pages(
            "/api/v2/get-session-list",
            {
                "session_status": "0",
                "sys_user_id": str(agent_id),
                "start_time": time_range,
            },
            page_size=100,
            max_pages=200,
            cancel_check=cancel,
        )
        return items

    workers = min(DEFAULT_MAX_WORKERS, max(1, total))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, aid): aid for aid in agent_ids}
        for fut in as_completed(futures):
            _check(progress)
            for s in fut.result():
                sid = str(s.get("session_id") or "")
                uid = str(s.get("chat_user_id") or "")
                key = f"{sid}||{uid}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    sessions.append(s)
            done += 1
            if progress:
                progress.advance(
                    1,
                    f"会话请求 {done}/{total}，已合并 {len(sessions)} 条会话",
                )

    return sessions


def _fetch_tagged_contacts(
    config: Config,
    tag_value_ids: List[str],
    progress: ProgressCallback = None,
) -> Dict[str, dict]:
    """按标签值 ID 拉取客户（等同手动导出「高值」表）。"""
    if not tag_value_ids:
        return {}

    cancel = _cancel_check(progress)
    total = len(tag_value_ids)
    if progress:
        progress.set_stage(
            "第 1/3 步：拉取标签客户",
            total,
            "按访客标签批量获取客户…",
        )

    contacts_by_uid: Dict[str, dict] = {}
    for idx, value_id in enumerate(tag_value_ids, start=1):
        _check(progress)
        items, _ = _client(config).get_all_pages(
            "/api/v2/get-contact-list",
            {"labels": str(value_id)},
            page_size=100,
            max_pages=500,
            cancel_check=cancel,
        )
        for contact in items:
            uid = str(contact.get("chat_user_id") or "")
            if uid:
                contacts_by_uid[uid] = contact
        if progress:
            progress.advance(
                1,
                f"标签 {idx}/{total}，累计 {len(contacts_by_uid)} 位客户",
            )

    return contacts_by_uid


def _fetch_sessions_by_ids(
    config: Config,
    session_ids: List[str],
    progress: ProgressCallback = None,
    stage_label: str = "补充会话",
) -> List[dict]:
    if not session_ids:
        return []

    cancel = _cancel_check(progress)
    total = len(session_ids)
    sessions: List[dict] = []

    def _fetch_one(session_id: str) -> Optional[dict]:
        if cancel and cancel():
            return None
        try:
            data = _client(config).get(
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

    if progress:
        progress.set_stage(
            f"第 2/3 步：{stage_label}",
            total,
            "补充标签客户对应会话…",
        )

    workers = min(DEFAULT_MAX_WORKERS, max(1, total))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, sid): sid for sid in session_ids}
        for fut in as_completed(futures):
            _check(progress)
            session = fut.result()
            if session:
                sessions.append(session)
            done += 1
            if progress:
                progress.advance(1, f"补充会话 {done}/{total}")

    return sessions


def _fetch_contacts_for_uids(
    config: Config,
    chat_user_ids: Set[str],
    progress: ProgressCallback = None,
) -> Dict[str, dict]:
    """按 chat_user_id 补充客户资料（不使用 updated_time，避免空结果）。"""
    if not chat_user_ids:
        return {}

    cancel = _cancel_check(progress)
    uid_list = sorted(chat_user_ids)
    total = len(uid_list)
    out: Dict[str, dict] = {}

    def _fetch_one(uid: str) -> tuple[str, Optional[dict]]:
        if cancel and cancel():
            return uid, None
        try:
            data = _client(config).get(
                "/api/v2/get-contact-list",
                {
                    "chat_user_id": str(uid),
                    "page": "1",
                    "page_size": "1",
                },
            )
            items = data.get("list") or []
            return uid, items[0] if items else None
        except Exception:
            return uid, None

    workers = min(DEFAULT_MAX_WORKERS, max(1, total))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, uid): uid for uid in uid_list}
        for fut in as_completed(futures):
            _check(progress)
            uid, contact = fut.result()
            if contact:
                out[str(uid)] = contact
            done += 1

    return out


def _merge_sessions(
    agent_sessions: List[dict],
    extra_sessions: List[dict],
) -> List[dict]:
    merged: List[dict] = []
    seen: Set[str] = set()
    for session in agent_sessions + extra_sessions:
        sid = str(session.get("session_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        merged.append(session)
    return merged


def _fetch_messages_for_sessions(
    config: Config,
    session_ids: List[str],
    progress: ProgressCallback = None,
) -> Dict[str, List[dict]]:
    """按会话拉取完整聊天记录（不按 send_time 截断），供统计使用。"""
    if not session_ids:
        return {}

    cancel = _cancel_check(progress)
    total = len(session_ids)

    if progress:
        progress.set_stage(
            "第 3/3 步：拉取聊天记录",
            total,
            "按会话并发拉取完整消息…",
        )

    grouped: Dict[str, List[dict]] = {}

    def _fetch_one(session_id: str) -> tuple[str, List[dict]]:
        if cancel and cancel():
            return session_id, []
        client = _client(config)
        items, _ = client.get_all_pages(
            "/api/v2/get-all-message-list",
            {"session_id": session_id},
            page_size=DEFAULT_PAGE_SIZE,
            max_pages=100,
            cancel_check=cancel,
        )
        items.sort(key=lambda m: m.get("send_time") or 0)
        return session_id, items

    workers = min(DEFAULT_MAX_WORKERS, max(1, total))
    done = 0
    msg_count = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, sid): sid for sid in session_ids}
        for fut in as_completed(futures):
            _check(progress)
            sid, items = fut.result()
            if items:
                grouped[sid] = items
                msg_count += len(items)
            done += 1
            if progress:
                progress.advance(
                    1,
                    f"会话消息 {done}/{total}，共 {msg_count} 条",
                )

    return grouped


def _group_messages_by_session(messages: List[dict]) -> Dict[str, List[dict]]:
    grouped: Dict[str, List[dict]] = {}
    for msg in messages:
        sid = str(msg.get("chat_session_id") or "").strip()
        if not sid:
            continue
        grouped.setdefault(sid, []).append(msg)
    return grouped


def build_merged_dataframe(
    client: SaleSmartlyClient,
    agent_ids: List[int],
    member_id_to_name: Dict[int, str],
    selected_tag_names: Optional[List[str]] = None,
    progress: ProgressCallback = None,
) -> "pd.DataFrame":
    import pandas as pd

    selected_tag_names = selected_tag_names or []
    tag_hint = "、".join(selected_tag_names) if selected_tag_names else "（未选）"
    config = client.config
    agent_set = set(agent_ids)
    window_start, window_end = business_window_from_tags(selected_tag_names)

    tag_value_id_map = build_tag_value_id_map(client)
    id_to_name = {vid: name for name, vid in tag_value_id_map.items()}
    tag_value_ids = _selected_tag_value_ids(selected_tag_names, tag_value_id_map)

    tagged_contacts = _fetch_tagged_contacts(config, tag_value_ids, progress)
    agent_sessions = _fetch_sessions_for_agents(
        config, window_start, window_end, agent_ids, progress
    )

    known_sids = {str(s.get("session_id") or "") for s in agent_sessions if s.get("session_id")}
    extra_ids: List[str] = []
    for contact in tagged_contacts.values():
        sid = str(contact.get("session_id") or "")
        if sid and sid not in known_sids:
            extra_ids.append(sid)

    extra_sessions = _fetch_sessions_by_ids(
        config, extra_ids, progress, stage_label="补充标签会话"
    )
    all_sessions = _merge_sessions(agent_sessions, extra_sessions)

    if not all_sessions:
        raise ValueError(
            f"所选访客标签（{tag_hint}）和客服下没有会话数据。\n"
            "请换一个标签或客服分组再试。"
        )

    contacts_map: Dict[str, dict] = dict(tagged_contacts)
    missing_uids = {
        str(s.get("chat_user_id"))
        for s in all_sessions
        if s.get("chat_user_id") and str(s.get("chat_user_id")) not in contacts_map
    }
    contacts_map.update(_fetch_contacts_for_uids(config, missing_uids))

    filtered_sessions: List[dict] = []
    for session in all_sessions:
        try:
            agent_id = int(session.get("sys_user_id") or 0)
        except (TypeError, ValueError):
            agent_id = 0
        if agent_id not in agent_set:
            continue

        chat_user_id = str(session.get("chat_user_id") or "")
        contact = contacts_map.get(chat_user_id, {})
        labels_str = _labels_for_row(session, contact, id_to_name)

        if selected_tag_names:
            in_tagged_customers = chat_user_id in tagged_contacts
            tag_ok = _contact_matches_tags(labels_str, selected_tag_names) or _session_matches_tag_ids(
                session, tag_value_ids
            )
            if not (in_tagged_customers or tag_ok):
                continue

        filtered_sessions.append(session)

    if not filtered_sessions:
        raise ValueError(
            "筛选后没有符合条件的会话。\n"
            f"当前访客标签：{tag_hint}\n"
            "可尝试换一个标签或客服分组。"
        )

    session_ids = [str(s.get("session_id") or "") for s in filtered_sessions if s.get("session_id")]
    messages_by_session = _fetch_messages_for_sessions(
        config, session_ids, progress
    )

    rows = []
    for session in filtered_sessions:
        chat_user_id = str(session.get("chat_user_id") or "")
        session_id = str(session.get("session_id") or "")
        contact = contacts_map.get(chat_user_id, {})
        contact_name = (
            str(contact.get("name") or "").strip()
            or str(session.get("title") or "").strip()
            or chat_user_id
        )
        labels_str = _labels_for_row(session, contact, id_to_name)

        msgs = messages_by_session.get(session_id, [])
        message_text = "\n".join(
            _format_message_line(m, member_id_to_name)
            for m in msgs
            if _should_include_in_transcript(m)
        )

        sys_user_id = session.get("sys_user_id")
        try:
            agent_name = member_id_to_name.get(int(sys_user_id), "")
        except (TypeError, ValueError):
            agent_name = ""
        if not agent_name and sys_user_id not in (None, 0, "0"):
            agent_name = str(sys_user_id)

        channel_name = CHANNEL_MAP.get(session.get("channel"), str(session.get("channel") or ""))
        session_tags = _format_session_tags(session.get("tags"))
        start_time = _format_ts(session.get("start_time"))
        assign_time = _format_ts(session.get("assign_time"))

        rows.append(
            {
                "联系人": contact_name,
                "会话ID": session_id,
                "访客标签": labels_str,
                "会话备注": str(contact.get("remark") or ""),
                "会话标签": session_tags,
                "用户评分": session.get("score", ""),
                "客户反馈": "",
                "接待员": agent_name,
                "接待时间": assign_time or start_time,
                "渠道": channel_name,
                "渠道信息": str(contact.get("channel_info") or ""),
                "会话生成时间": start_time,
                "描述": str(contact.get("remark_name") or ""),
                "接待成员": agent_name,
                "社媒渠道": channel_name,
                "会话消息内容": message_text,
            }
        )

    df = pd.DataFrame(rows)
    df.columns = df.columns.str.strip()
    return df
