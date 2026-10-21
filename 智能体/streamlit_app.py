# -*- coding: utf-8 -*-
"""
大力丸 · 聊天记录质检工具（网页版 Streamlit）
核心逻辑复用 qc_core.py，与桌面版共用。
"""

import io
import importlib
import os
import re
import sys
import time
from datetime import datetime, timedelta

# 保证始终加载本目录下的 qc_core.py（避免从其他路径导入旧版本）
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import pandas as pd
import streamlit as st

import qc_core as core

# Streamlit 长驻进程会缓存已导入模块；开发时 reload 以拾取 qc_core 更新
importlib.reload(core)

def _reload_fetch_qc_salesmartly():
    """Streamlit 长驻进程会缓存模块；拉取前 reload 以拾取 fetch 相关更新。"""
    import fetch_deal_salesmartly as deal_mod
    import fetch_qc_salesmartly as mod
    importlib.reload(deal_mod)
    importlib.reload(mod)
    return mod

# 分析范围常量（与 qc_core 同步）
SCOPE_TODAY = getattr(core, "TIME_SCOPE_TODAY", "today")
SCOPE_ALL = getattr(core, "TIME_SCOPE_ALL", "all")
SCOPE_CUSTOM = getattr(core, "TIME_SCOPE_CUSTOM", "custom")
SCOPE_LABELS = getattr(
    core,
    "TIME_SCOPE_LABELS",
    {SCOPE_ALL: "全部", SCOPE_TODAY: "当天", SCOPE_CUSTOM: "自定义"},
)

st.set_page_config(
    page_title="聊天记录质检 V1.5",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)

COLUMN_FIELDS = [
    ("id", "会话 ID"),
    ("time", "发言时间"),
    ("sender", "发送人"),
    ("msg", "消息内容"),
    ("contact", "联系人"),
    ("member", "接待成员"),
    ("channel", "渠道"),
]

REQUIRED_FIELDS = {"msg"}


def _show_table(df) -> None:
    """用 st.table 展示，避免 st.dataframe 懒加载 DataFrame.js 在缓存异常时失败。"""
    if df is None:
        st.caption("（无数据）")
        return
    try:
        show = df.head(200) if hasattr(df, "head") else df
    except Exception:
        show = df
    if getattr(show, "empty", False):
        st.caption("（无数据）")
        return
    st.table(show)


def _show_stat_row(stats: list[tuple[str, object]]) -> None:
    """用 markdown 展示统计行，避免 st.metric 懒加载 Metric.js 在缓存异常时失败。"""
    if not stats:
        return
    cols = st.columns(len(stats))
    for col, (label, value) in zip(cols, stats):
        with col:
            st.markdown(f"**{label}**  \n{value}")


def _init_state():
    # 清理旧版 widget key，避免 Streamlit DOM 冲突
    st.session_state.pop("time_scope_radio", None)

    defaults = {
        "uploaded_dfs": None,
        "column_overrides": {},
        "report_bytes": None,
        "report_name": None,
        "last_stats": None,
        "last_timing": None,
        "last_deep_review": None,
        "last_deep_timing": None,
        "last_quality_df": None,
        "last_low_df": None,
        "last_deep_df": None,
        "last_window_label": None,
        "last_filter_df": None,
        "last_filter_total": None,
        "report_kind": "qc",
        # 聊天进度对比
        "compare_report_metas": None,
        "compare_rows": None,
        "compare_df": None,
        "compare_stats": None,
        "compare_report_bytes": None,
        "compare_report_name": None,
        "deal_last_import_summary": None,
        "deal_last_import_target": None,
        "deal_last_learn_result": None,
        "deal_learn_skipped": False,
        "kb_review_md": "",
        "kb_parsed_preview": None,
        "kb_supplement_preview": None,
        "kb_refine_result": None,
        "kb_refined_rules": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _scope_label(scope):
    return SCOPE_LABELS.get(scope, scope)


def _init_custom_window_defaults():
    """首次进入「自定义」时，用营业日窗口作为默认起止时间。"""
    try:
        start, end = core.get_business_day_window()
    except AttributeError:
        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    defaults = {
        "custom_start_date": start.date(),
        "custom_start_time": start.time().replace(second=0, microsecond=0),
        "custom_end_date": end.date(),
        "custom_end_time": end.time().replace(second=0, microsecond=0),
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _get_custom_window():
    """从 session_state 组合出自定义 (start, end)；不完整时返回 None。"""
    sd = st.session_state.get("custom_start_date")
    stime = st.session_state.get("custom_start_time")
    ed = st.session_state.get("custom_end_date")
    etime = st.session_state.get("custom_end_time")
    if not (sd and ed):
        return None
    start = datetime.combine(sd, stime or datetime.min.time())
    end = datetime.combine(ed, etime or datetime.min.time())
    return start, end


@st.fragment
def _time_scope_fragment():
    """侧栏分析范围内容；fragment 内不可使用 st.sidebar，由外层 with 块注入。"""
    try:
        start, end = core.get_business_day_window()
        window_text = core.format_business_day_window(start, end)
    except AttributeError:
        window_text = "前一天 20:00 至当天 20:00"

    scope_options = [SCOPE_TODAY, SCOPE_ALL, SCOPE_CUSTOM]
    if st.session_state.get("time_scope") not in scope_options:
        st.session_state.time_scope = SCOPE_TODAY

    st.markdown("### 分析范围")
    choice = st.radio(
        "分析范围",
        options=scope_options,
        format_func=_scope_label,
        key="time_scope",
        label_visibility="collapsed",
    )

    if choice == SCOPE_TODAY:
        st.markdown(f"按消息行内 `[时间] 客户 : ...` 筛选，窗口：**{window_text}**")
    elif choice == SCOPE_ALL:
        st.markdown("使用每通会话的**完整**聊天记录。")
    else:
        st.markdown("自定义时间窗口（左闭右开），按消息行内 `[时间]` 筛选：")
        _init_custom_window_defaults()
        c1, c2 = st.columns(2)
        with c1:
            st.date_input("开始日期", key="custom_start_date")
            st.time_input("开始时间", key="custom_start_time", step=timedelta(minutes=5))
        with c2:
            st.date_input("结束日期", key="custom_end_date")
            st.time_input("结束时间", key="custom_end_time", step=timedelta(minutes=5))
        cw = _get_custom_window()
        if cw is not None:
            s, e = cw
            if s >= e:
                st.warning("结束时间需晚于开始时间。")
            else:
                st.caption(f"窗口：**{core.format_business_day_window(s, e)}**")


def _render_time_scope_selector():
    """在 sidebar 上下文中调用 fragment，切换范围时仅重绘侧栏。"""
    with st.sidebar:
        _time_scope_fragment()


def _load_sessions_scoped(file_dfs, overrides):
    scope = st.session_state.get("time_scope", SCOPE_TODAY)
    # 自定义范围：先加载完整会话（不过滤），再按自定义窗口筛选。
    load_scope = SCOPE_ALL if scope == SCOPE_CUSTOM else scope
    try:
        result = core.load_sessions(
            file_dfs=file_dfs,
            column_map=overrides,
            time_scope=load_scope,
        )
    except TypeError:
        result = core.load_sessions(file_dfs=file_dfs, column_map=overrides)

    if len(result) == 3:
        sessions, diag, window = result
    else:
        sessions, diag = result
        if load_scope != SCOPE_ALL and hasattr(core, "apply_time_scope_to_sessions"):
            sessions, window = core.apply_time_scope_to_sessions(sessions, load_scope)
        else:
            window = None

    if scope == SCOPE_CUSTOM:
        cw = _get_custom_window()
        if cw is not None and cw[0] < cw[1]:
            sessions, window = core.filter_sessions_by_window(sessions, cw[0], cw[1])
        else:
            window = None

    st.session_state.last_window_label = window["label"] if window else None
    return sessions, diag, window


@st.cache_data(ttl=300, show_spinner=False)
def _cached_salesmartly_members():
    from fetch_deal_salesmartly import build_member_groups, build_member_maps, fetch_members
    from salesmartly_client import ConfigError, SaleSmartlyClient, load_config

    cfg = load_config()
    client = SaleSmartlyClient(cfg)
    members = fetch_members(client)
    return members, build_member_groups(members), build_member_maps(members)


def _resolve_qc_api_window(time_scope: str):
    """API 拉数用时间窗；全部模式返回 None。"""
    if time_scope == SCOPE_ALL:
        return None
    if time_scope == SCOPE_CUSTOM:
        cw = _get_custom_window()
        if cw is None or cw[0] >= cw[1]:
            return None
        return cw
    start, end = core.get_business_day_window()
    return start, end


def _get_selected_qc_agent_ids(member_groups: dict) -> list[int]:
    selected: list[int] = []
    for _gname, members in member_groups.items():
        for uid, _name in members:
            if st.session_state.get(f"qc_agent_{uid}"):
                selected.append(uid)
    return selected


def _select_qc_agent_group(member_ids):
    """在 widget 回调中选中整组，允许更新已渲染过的客服复选框。"""
    for uid in member_ids:
        st.session_state[f"qc_agent_{uid}"] = True


def _render_qc_agent_selector(member_groups: dict, member_id_to_name: dict) -> list[int]:
    from fetch_deal_salesmartly import sort_member_group_names

    st.markdown("**接待客服**（至少选 1 人，只分析所选客服的客户）")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("全选客服", key="qc_agents_select_all"):
            for _gname, members in member_groups.items():
                for uid, _ in members:
                    st.session_state[f"qc_agent_{uid}"] = True
            st.rerun()
    with c2:
        if st.button("清空客服", key="qc_agents_clear"):
            for _gname, members in member_groups.items():
                for uid, _ in members:
                    st.session_state[f"qc_agent_{uid}"] = False
            st.rerun()

    if not member_groups:
        st.warning("未获取到客服列表，请检查 api-key.json。")
        return []

    seen_uids: set[int] = set()
    for gname in sort_member_group_names(member_groups.keys()):
        members = member_groups[gname]
        group_col, select_col = st.columns([8, 1])
        with select_col:
            st.button(
                "全选本组",
                key=f"qc_agents_select_group_{gname}",
                on_click=_select_qc_agent_group,
                args=([uid for uid, _ in members],),
                help=f"选中「{gname}」中的所有客服，保留其他组的选择。",
            )
        with group_col:
            with st.expander(f"{gname}（{len(members)} 人）", expanded=gname in ("一转高", "一转西")):
                cols = st.columns(3)
                col_i = 0
                for uid, name in members:
                    if uid in seen_uids:
                        continue
                    seen_uids.add(uid)
                    with cols[col_i % 3]:
                        st.checkbox(name, key=f"qc_agent_{uid}")
                    col_i += 1


    return _get_selected_qc_agent_ids(member_groups)


def _salesmartly_fetch_progress_ui(cancel_check=None):
    """SaleSmartly 拉取进度条与最近日志；cancel_check 用于进度回调中检测断连。"""
    from fetch_cancel import raise_if_cancelled

    progress = st.progress(0, text="准备拉取 SaleSmartly 数据…")
    status = st.empty()
    logs: list[str] = []

    def on_progress(msg: str) -> None:
        raise_if_cancelled(cancel_check)
        logs.append(msg)
        if len(logs) > 8:
            logs.pop(0)
        status.markdown("**拉取进度**\n\n" + "\n\n".join(f"- {l}" for l in logs))

        pct = 0.0
        bar_text = msg
        if m := re.search(r"聊天记录 (\d+)/(\d+)", msg):
            done, total = int(m.group(1)), max(int(m.group(2)), 1)
            pct = 0.45 + 0.55 * (done / total)
            bar_text = f"拉取聊天记录 {done}/{total}"
        elif m := re.search(r"客服客户列表 (\d+)/(\d+)", msg):
            done, total = int(m.group(1)), max(int(m.group(2)), 1)
            pct = 0.08 + 0.22 * (done / total)
            bar_text = f"拉取客户列表 {done}/{total}"
        elif m := re.search(r"会话详情 (\d+)/(\d+)", msg):
            done, total = int(m.group(1)), max(int(m.group(2)), 1)
            pct = 0.32 + 0.12 * (done / total)
            bar_text = f"拉取会话详情 {done}/{total}"
        elif "查找日期标签" in msg or "拉取标签客户" in msg:
            pct = 0.03
        elif "并行拉取" in msg and "客服" in msg:
            pct = 0.05
        elif "合并后" in msg or "日期标签下" in msg:
            pct = 0.30
        elif "客户最近回复时间预筛" in msg:
            pct = 0.28
        elif "拉取" in msg and "个会话" in msg:
            pct = 0.32
        elif "完整聊天" in msg:
            pct = 0.42
        elif "完成" in msg:
            pct = 1.0

        progress.progress(min(pct, 1.0), text=bar_text)

    return on_progress, progress


def _begin_salesmartly_fetch():
    """初始化拉取 UI，返回 (on_progress, progress_bar, cancel_check)。"""
    from streamlit_fetch_cancel import make_streamlit_cancel_check

    st.caption("关闭本浏览器标签页将自动停止拉取。")
    cancel_check, _ = make_streamlit_cancel_check()
    on_progress, progress = _salesmartly_fetch_progress_ui(cancel_check)
    return on_progress, progress, cancel_check


def _qc_concurrency_control(cfg: dict) -> int:
    model = cfg.get("model") or core.DEFAULTS.get("model")
    max_conc = core.max_concurrency_for_model(model)
    default_conc = int(cfg.get("concurrency") or core.default_concurrency_for_model(model))
    default_conc = max(1, min(default_conc, max_conc))
    st.number_input(
        "AI 分析并发数",
        min_value=1,
        max_value=max_conc,
        value=default_conc,
        step=1,
        key="qc_concurrency",
        help=(
            f"当前模型 **{model}** 账号级并发上限约 **{max_conc}**（见 "
            "[DeepSeek 限速文档](https://api-docs.deepseek.com/zh-cn/quick_start/rate_limit)）。"
            "过高可能触发 HTTP 429。"
        ),
    )
    return int(st.session_state.get("qc_concurrency") or default_conc)


def _execute_customer_filter(sessions, time_scope, window=None):
    """生成仅四列的筛选报告，不进入 AI 质检、经验检索或质检结果入库。"""
    df = core.build_customer_filter_dataframe(sessions)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="客户筛选", index=False)
    scope_tag = _scope_label(time_scope)
    st.session_state.update({
        "last_filter_df": df,
        "last_filter_total": len(sessions),
        "last_stats": None,
        "last_timing": None,
        "last_deep_review": None,
        "last_deep_timing": None,
        "last_quality_df": None,
        "last_low_df": None,
        "last_deep_df": None,
        "last_window_label": window["label"] if window else None,
        "report_kind": "filter",
        "report_sheet_counts": None,
        "report_bytes": buf.getvalue(),
        "report_name": f"客户筛选_{scope_tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        "report_scope": scope_tag,
    })
    st.success(f"筛选完成：共 {len(sessions)} 通会话，保留发言超过 3 句的 {len(df)} 通，未使用 AI。")


def _execute_qc_run(
    cfg: dict,
    sessions: list,
    time_scope: str,
    *,
    concurrency: int | None = None,
    window=None,
    filter_only: bool = False,
):
    """执行质检主流程（Excel / API 共用）。"""
    if not sessions:
        st.error("没有可分析的会话。")
        return

    if window:
        st.caption(f"已按发言时间过滤（{window['label']}），共 {len(sessions)} 通会话")

    if filter_only:
        _execute_customer_filter(sessions, time_scope, window)
        return

    enable_deal_context = st.session_state.get("enable_deal_context", True)
    enable_deep_review = st.session_state.get("enable_deep_review", False)
    run_ab_validation = st.session_state.get("run_ab_validation", False)

    progress = st.progress(0, text="准备中…")
    status = st.empty()
    logs = []

    def on_progress(done, total, row, tier):
        pct = done / total if total else 0
        progress.progress(pct, text=f"质检进度 {done}/{total}")
        if tier == "skip":
            line = (
                f"[{done}/{total}] {row.get('联系人','')} → [规则筛选] "
                f"客户{row.get('客户发言数','')}条/客服{row.get('客服发言数','')}条 · {row.get('快速筛选','')}"
            )
        else:
            err = row.get("_错误", "")
            if err:
                err = core.redact_secrets(err, cfg)
                line = f"[{done}/{total}] {row.get('联系人','')} → ❌ {err}"
            else:
                line = (
                    f"[{done}/{total}] {row.get('联系人','')} → "
                    f"[{row.get('质检档位')}] {row.get('结果标签','')} / "
                    f"{row.get('是否合格','')} / 风险{row.get('风险等级','')}"
                )
        logs.append(line)
        if len(logs) > 8:
            logs.pop(0)
        status.markdown("**最近进度**\n\n" + "\n\n".join(f"- {l}" for l in logs))

    with st.spinner("正在质检，请稍候…"):
        try:
            qc_cfg = dict(cfg)
            qc_cfg["deal_context_enabled"] = enable_deal_context
            if concurrency is not None:
                qc_cfg["concurrency"] = max(1, int(concurrency))
            timing_raw = {}
            results = core.run_qc_batch(
                sessions, qc_cfg, on_progress=on_progress, timing_out=timing_raw
            )
        except Exception as e:
            st.error(f"质检过程异常：{core.redact_secrets(str(e), cfg)}")
            return

    if run_ab_validation and sessions:
        with st.expander("效果验证：有/无成交参考对比（首通）", expanded=True):
            try:
                ab = core.compare_qc_deal_context(qc_cfg, sessions[0])
                st.caption(
                    f"参考案例：{ab.get('deal_ref_summary') or '无'} · "
                    f"档位：{ab.get('tier', '')}"
                )
                diffs = ab.get("diffs") or []
                if not diffs:
                    st.success("两次质检关键字段一致。")
                else:
                    st.warning(f"有 {len(diffs)} 个字段不同：")
                    st.dataframe(diffs, use_container_width=True)
            except Exception as e:
                st.error(f"对比失败：{core.redact_secrets(str(e), cfg)}")

    progress.progress(1.0, text="常规质检完成")
    stats = core.compute_stats(results)
    timing = core.summarize_qc_timing(timing_raw)
    st.session_state.last_stats = stats
    st.session_state.last_timing = timing

    deep_rows = []
    deep_timing_raw = {}
    if enable_deep_review:
        eligible_n = sum(1 for s in sessions if core.qualifies_for_deep_review(s))
        if eligible_n:
            progress.progress(0, text=f"深度复盘 0/{eligible_n}")

            def on_deep_progress(done, total, row):
                pct = done / total if total else 0
                progress.progress(pct, text=f"深度复盘 {done}/{total}")
                contact = row.get("联系人", "")
                err = row.get("_错误", "")
                if err:
                    line = f"[深度 {done}/{total}] {contact} → ❌ {core.redact_secrets(err, cfg)}"
                else:
                    line = (
                        f"[深度 {done}/{total}] {contact} → "
                        f"{row.get('卡点诊断', '')[:40]}…"
                    )
                logs.append(line)
                if len(logs) > 8:
                    logs.pop(0)
                status.markdown("**最近进度**\n\n" + "\n\n".join(f"- {l}" for l in logs))

            with st.spinner(f"正在深度复盘 {eligible_n} 通会话…"):
                raw_deep = core.run_deep_review_batch(
                    sessions,
                    qc_cfg,
                    on_progress=on_deep_progress,
                    timing_out=deep_timing_raw,
                )
            deep_rows = [r for r in raw_deep if r]
            progress.progress(1.0, text="全部完成")
        else:
            st.info("无客户发言超过 10 条的会话，已跳过深度复盘。")

    st.session_state.last_deep_review = deep_rows
    if deep_timing_raw:
        wall = deep_timing_raw.get("wall_seconds", 0)
        cnt = deep_timing_raw.get("count", 0)
        st.session_state.last_deep_timing = {
            "数量": cnt,
            "总耗时文本": core.format_duration(wall),
            "单通均价文本": core.format_duration(wall / cnt) if cnt else "—",
        }
    else:
        st.session_state.last_deep_timing = None

    df_quality, df_low = core.split_report_results(results)
    df_deep = core.build_deep_review_dataframe(deep_rows) if deep_rows else None
    st.session_state.last_quality_df = df_quality
    st.session_state.last_low_df = df_low
    st.session_state.last_deep_df = df_deep

    buf = io.BytesIO()
    core.write_report_excel(results, buf, deep_review_rows=deep_rows)
    st.session_state.report_bytes = buf.getvalue()
    st.session_state.report_sheet_counts = (
        len(df_quality),
        len(df_low),
        len(deep_rows),
    )
    scope_tag = _scope_label(time_scope)
    st.session_state.report_name = f"质检报告_{scope_tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    st.session_state.report_scope = scope_tag
    st.session_state.report_kind = "qc"
    st.session_state.last_filter_df = None
    st.session_state.last_filter_total = None

    done_msg = f"质检完成（{scope_tag}）"
    if deep_rows:
        done_msg += f"，深度复盘 **{len(deep_rows)}** 通"
    st.success(done_msg)

    try:
        from db import save_qc_results, mark_deal_customers_from_qc

        save_result = save_qc_results(results)
        deal_result = mark_deal_customers_from_qc(results)
        delete_msg = ""
        try:
            from db import mark_deleted_customers_from_qc

            delete_result = mark_deleted_customers_from_qc(results)
            delete_msg = f"；标记删除客户：{delete_result['deleted_customers']} 个"
        except ImportError:
            delete_msg = "（删除客户标记未启用：请更新 db.py）"
        st.info(
            f"质检结果已入库：{save_result['saved']} 条；"
            f"新增/更新成交客户：{deal_result['deal_customers']} 个"
            f"{delete_msg}"
        )
        if save_result.get("skipped"):
            st.warning(
                f"有 {save_result['skipped']} 条质检结果缺少会话ID，无法入库。"
            )
    except RuntimeError as e:
        st.warning(f"质检结果入库跳过（数据库不可用）：{e}")
    except Exception as e:
        st.warning(f"质检结果入库失败：{e}")


def _read_uploaded_files(uploaded_files):
    """读取上传的 Excel，失败时抛出带文件名的异常。"""
    file_dfs = []
    errors = []
    for uf in uploaded_files:
        try:
            df = pd.read_excel(io.BytesIO(uf.getvalue()), sheet_name=0, dtype=str)
            file_dfs.append((uf.name, df))
        except Exception as e:
            errors.append(f"· {uf.name}：{e}")
    if errors and not file_dfs:
        raise ValueError("所有文件读取失败：\n" + "\n".join(errors))
    return file_dfs, errors


def _default_override_for_cols(all_columns):
    detected = core.detect_columns(all_columns)
    overrides = {}
    opts = core.suggest_column_options(all_columns)
    for key, _label in COLUMN_FIELDS:
        col = detected.get(key)
        overrides[key] = col if col else "（不映射）"
    return overrides, opts, detected


def _build_column_map(all_columns, overrides):
    return core.resolve_column_map(core.detect_columns(all_columns), overrides)


def _check_app_password(cfg):
    """若配置了 app_password，要求访问密码（非 API Key）。"""
    pwd = (cfg.get("app_password") or "").strip()
    if not pwd:
        return True
    if st.session_state.get("app_authenticated"):
        return True
    st.subheader("访问验证")
    st.caption("本页面已启用访问密码，请联系管理员获取。")
    entered = st.text_input("访问密码", type="password", key="app_pwd_input")
    if st.button("进入", type="primary"):
        if entered == pwd:
            st.session_state.app_authenticated = True
            st.rerun()
        else:
            st.error("密码错误")
    return False


_CUSTOM_MODEL_OPTION = "自定义…"


def _select_model(cfg):
    """侧边栏选择模型；可从接口拉取列表，并支持配置列表与自定义输入。"""
    config_models = list(cfg.get("models") or [])
    default_model = cfg.get("model") or (config_models[0] if config_models else "")

    with st.sidebar:
        st.markdown("### 模型选择")

        if st.button("🔄 从接口获取模型列表", use_container_width=True):
            with st.spinner("正在获取模型列表…"):
                fetched, err = core.fetch_available_models(cfg)
            if err:
                st.session_state.model_fetch_error = err
                st.session_state.fetched_models = []
            else:
                st.session_state.model_fetch_error = None
                st.session_state.fetched_models = fetched
                st.toast(f"获取到 {len(fetched)} 个模型")

        fetched_models = st.session_state.get("fetched_models") or []
        if st.session_state.get("model_fetch_error"):
            st.caption(f"⚠️ 获取失败：{st.session_state.model_fetch_error}")
        elif fetched_models:
            st.caption(f"接口模型：{len(fetched_models)} 个")

        # 合并优先级：接口模型 > 配置模型 > 当前默认
        merged = []
        for m in [*fetched_models, *config_models, default_model]:
            if m and m not in merged:
                merged.append(m)
        options = merged + [_CUSTOM_MODEL_OPTION]

        if st.session_state.get("model_choice") not in options:
            st.session_state.model_choice = (
                default_model if default_model in options else options[0]
            )
        choice = st.selectbox(
            "模型",
            options=options,
            key="model_choice",
            label_visibility="collapsed",
        )
        if choice == _CUSTOM_MODEL_OPTION:
            custom = st.text_input(
                "自定义模型名",
                value=st.session_state.get("custom_model", default_model),
                key="custom_model",
                placeholder="例如 deepseek-chat",
            ).strip()
            chosen = custom or default_model
        else:
            chosen = choice
        st.caption(f"当前模型：**{chosen}**")
    return chosen


def main():
    _init_state()
    st.title("聊天记录质检（网页版）V1.5")

    # ---------- 配置检查（qc_config.json → st.secrets）----------
    try:
        secrets = st.secrets
    except Exception:
        secrets = None
    cfg, cfg_warn, cfg_source = core.load_config_for_streamlit(secrets)

    if cfg_warn:
        st.error("管理员未配置 API Key")
        st.markdown(cfg_warn)
        st.stop()

    selected_model = _select_model(cfg)
    cfg["model"] = selected_model

    source_label = "本地配置文件" if cfg_source == "file" else "Streamlit Secrets"
    st.success(
        f"已加载配置（{source_label}）· 模型 **{selected_model}** · "
        f"并发 **{cfg.get('concurrency')}** · API Key：已配置"
    )

    if not _check_app_password(cfg):
        st.stop()

    qc_tab, deal_tab, delete_tab, kb_tab, compare_tab = st.tabs(
        ["聊天质检", "上传成交客户", "上传删除客户", "知识库管理", "聊天进度对比"]
    )
    with qc_tab:
        _render_qc_tab(cfg)
    with deal_tab:
        _render_deal_import_tab(cfg)
    with delete_tab:
        _render_delete_import_tab(cfg)
    with kb_tab:
        _render_knowledge_base_tab(cfg)
    with compare_tab:
        _render_progress_compare_tab(cfg)


def _render_qc_tab(cfg):
    st.caption(
        "从 SaleSmartly API 拉取或上传 Excel → 筛选会话 → AI 质检 → 下载报告。"
        " API Key 由管理员在服务器本地配置。"
    )

    _render_time_scope_selector()
    time_scope = st.session_state.get("time_scope", SCOPE_TODAY)

    if time_scope == SCOPE_CUSTOM:
        cw = _get_custom_window()
        if cw is None or cw[0] >= cw[1]:
            st.warning("请在左侧边栏设置有效的「自定义」起止时间（结束需晚于开始）。")

    source = st.radio(
        "数据来源",
        ["SaleSmartly API", "上传 Excel"],
        horizontal=True,
        key="qc_data_source",
    )

    file_dfs = None
    overrides = {}
    api_ready = False

    if source.startswith("SaleSmartly"):
        st.subheader("1. 从 SaleSmartly 拉取")
        api_window = _resolve_qc_api_window(time_scope)
        if time_scope == SCOPE_CUSTOM and api_window is None:
            st.stop()
        if api_window:
            st.caption(
                f"筛选：**客户最近回复时间**在 **{core.format_business_day_window(api_window[0], api_window[1])}**；"
                "仅拉取这些客户的完整聊天记录进入质检。"
            )
        else:
            st.caption("分析范围为「全部」：不按最后沟通时间筛，拉取所选客服下的客户全量聊天。")

        try:
            members, member_groups, member_id_to_name = _cached_salesmartly_members()
        except Exception as e:
            st.error(f"无法加载客服列表：{core.redact_secrets(str(e), cfg)}")
            st.caption("请确认 `智能体/api-key.json` 已配置。")
            st.stop()

        agent_ids = _render_qc_agent_selector(member_groups, member_id_to_name)
        api_ready = bool(agent_ids)

        if st.button("预览拉取结果", type="secondary", key="qc_api_preview"):
            if not agent_ids:
                st.warning("请至少选择 1 位接待客服。")
            else:
                from fetch_cancel import FetchCancelledError
                fetch_qc_mod = _reload_fetch_qc_salesmartly()
                fetch_qc_dataframe = fetch_qc_mod.fetch_qc_dataframe
                dataframe_to_sessions = fetch_qc_mod.dataframe_to_sessions
                from salesmartly_client import SaleSmartlyClient, load_config

                on_fetch_progress, fetch_bar, cancel_check = _begin_salesmartly_fetch()
                sessions = []
                meta = {}
                window = None
                try:
                    client = SaleSmartlyClient(load_config())
                    df, meta = fetch_qc_dataframe(
                        client,
                        agent_ids,
                        window=api_window,
                        require_customer_speech=True,
                        on_progress=on_fetch_progress,
                        cancel_check=cancel_check,
                    )
                    custom_w = api_window if time_scope == SCOPE_CUSTOM else None
                    sessions, window = dataframe_to_sessions(
                        df, time_scope=time_scope, custom_window=custom_w
                    )
                    fetch_bar.progress(1.0, text="拉取完成")
                except FetchCancelledError:
                    st.warning("拉取已取消（页面已关闭或连接已断开）。")
                except Exception as e:
                    st.error(f"拉取失败：{core.redact_secrets(str(e), cfg)}")
                    sessions = []
                    meta = {}
                    window = None

                if meta or sessions:
                    st.caption(
                        f"API客户 {meta.get('contacts_from_api', meta.get('contacts_total', 0))} · "
                        f"客户回复筛选后 {meta.get('contacts_total', 0)} · "
                        f"会话 {meta.get('sessions', 0)} · "
                        f"保留 {meta.get('sessions_kept', len(sessions))} · "
                        f"跳过销售单向触达 {meta.get('skipped_no_customer_reply', 0)} · "
                        f"聊天记录校验跳过 {meta.get('skipped_no_customer_speech', 0)}"
                    )
                if sessions:
                    scope_note = f"（{_scope_label(time_scope)}"
                    if window:
                        scope_note += f" · {window['label']}"
                    scope_note += "）"
                    st.success(f"预计分析 **{len(sessions)}** 通会话{scope_note}")
                else:
                    st.warning("没有符合筛选条件的会话。")
    else:
        st.subheader("1. 上传 Excel")
        uploaded = st.file_uploader(
            "选择一个或多个聊天记录 .xlsx",
            type=["xlsx"],
            accept_multiple_files=True,
            key="qc_excel_upload",
        )

        if not uploaded:
            st.info(
                "请上传聊天记录表格。表头通常含：会话ID、联系人、接待成员、社媒渠道、会话消息。"
            )
        else:
            try:
                file_dfs, read_errors = _read_uploaded_files(uploaded)
            except ValueError as e:
                st.error(str(e))
                file_dfs = None
                read_errors = []

            if file_dfs:
                for err in read_errors:
                    st.warning(err)
                st.session_state.uploaded_dfs = file_dfs

                preview_name, preview_df = file_dfs[0]
                st.subheader("2. 数据预览")
                with st.expander(
                    f"展开查看数据预览（{preview_name} · 共 {len(preview_df)} 行 · 已上传 {len(file_dfs)} 个文件）",
                    expanded=False,
                ):
                    st.dataframe(preview_df.head(20), use_container_width=True)

                st.subheader("3. 列名映射（可手动调整）")
                all_columns = list(preview_df.columns)
                if not st.session_state.column_overrides:
                    st.session_state.column_overrides, opts, auto_det = _default_override_for_cols(all_columns)
                else:
                    opts = core.suggest_column_options(all_columns)
                    auto_det = core.detect_columns(all_columns)

                with st.expander("展开调整列名映射（「消息内容」为必填）", expanded=False):
                    cols_ui = st.columns(4)
                    overrides = {}
                    for i, (key, label) in enumerate(COLUMN_FIELDS):
                        with cols_ui[i % 4]:
                            current = st.session_state.column_overrides.get(key, "（不映射）")
                            if current not in opts:
                                current = "（不映射）"
                            overrides[key] = st.selectbox(
                                label,
                                opts,
                                index=opts.index(current) if current in opts else 0,
                                key=f"colmap_{key}",
                            )
                    st.session_state.column_overrides = overrides
                    if st.checkbox("查看自动识别结果", value=False, key="show_auto_detect"):
                        st.json(auto_det)

                column_map = _build_column_map(all_columns, overrides)
                if not column_map.get("msg") or overrides.get("msg") == "（不映射）":
                    st.warning("请映射「消息内容」列后再开始质检。")
                elif st.button("预览解析结果", type="secondary", key="qc_excel_preview"):
                    try:
                        sessions, diag, window = _load_sessions_scoped(file_dfs, overrides)
                        if not sessions:
                            hint = "当天窗口内没有可分析的消息" if window else "未能解析出任何会话"
                            st.error(f"{hint}，请检查列映射、时间列或分析范围。")
                        else:
                            scope_note = f"（{_scope_label(time_scope)}"
                            if window:
                                scope_note += f" · {window['label']}"
                            scope_note += "）"
                            st.success(f"预计分析 **{len(sessions)}** 通会话{scope_note}")
                    except Exception as e:
                        st.error(f"解析失败：{e}")

    st.subheader("4. 开始质检")
    filter_only = st.toggle(
        "仅筛选客户，不使用 AI（客户发言超过 3 句）",
        value=False,
        key="qc_filter_only",
    )
    concurrency = None
    if filter_only:
        st.caption(
            "按当前分析范围保留有效客户发言至少 4 条的会话（沿用现有计数规则，图片和单独的“1”不计入）。"
            "不调用 AI；导出仅含客户发言时间、客户发言次数、客户名字、接待客服名字。"
            "客户发言时间取范围内最近一次有效发言时间。"
        )
    else:
        concurrency = _qc_concurrency_control(cfg)
        st.caption("SaleSmartly 拉数默认 10 路并行；AI 质检按上方并发数执行。")
        st.toggle(
            "启用深度复盘（客户发言>10条；常规质检全部完成后再执行）",
            value=False,
            key="enable_deep_review",
        )
        st.toggle(
            "注入成交/删除经验参考（检索相似已成交与已删除案例，辅助质检判断）",
            value=bool(cfg.get("deal_context_enabled", True)),
            key="enable_deal_context",
        )
        st.checkbox(
            "效果验证：首通会话对比「有/无成交参考」两次质检（多耗 1 次 API）",
            value=False,
            key="run_ab_validation",
        )
    start = st.button("开始筛选" if filter_only else "开始质检", type="primary", key="qc_start_btn")

    if start:
        if source.startswith("SaleSmartly"):
            if not api_ready:
                st.warning("请至少选择 1 位接待客服。")
            else:
                from fetch_cancel import FetchCancelledError
                fetch_qc_mod = _reload_fetch_qc_salesmartly()
                fetch_qc_dataframe = fetch_qc_mod.fetch_qc_dataframe
                dataframe_to_sessions = fetch_qc_mod.dataframe_to_sessions
                from salesmartly_client import SaleSmartlyClient, load_config

                api_window = _resolve_qc_api_window(time_scope)
                if time_scope == SCOPE_CUSTOM and api_window is None:
                    st.error("自定义时间窗口无效。")
                else:
                    agent_ids = _get_selected_qc_agent_ids(_cached_salesmartly_members()[1])
                    on_fetch_progress, fetch_bar, cancel_check = _begin_salesmartly_fetch()
                    sessions = None
                    try:
                        client = SaleSmartlyClient(load_config())
                        df, _meta = fetch_qc_dataframe(
                            client,
                            agent_ids,
                            window=api_window,
                            require_customer_speech=True,
                            on_progress=on_fetch_progress,
                            cancel_check=cancel_check,
                        )
                        custom_w = api_window if time_scope == SCOPE_CUSTOM else None
                        sessions, window = dataframe_to_sessions(
                            df, time_scope=time_scope, custom_window=custom_w
                        )
                        fetch_bar.progress(1.0, text="拉取完成")
                    except FetchCancelledError:
                        st.warning("拉取已取消（页面已关闭或连接已断开）。")
                    except Exception as e:
                        st.error(f"拉取失败：{core.redact_secrets(str(e), cfg)}")
                        sessions = None

                    if sessions is None:
                        pass
                    elif not sessions:
                        st.error("没有符合筛选条件的会话（请调整客服或分析范围）。")
                    else:
                        _execute_qc_run(
                            cfg,
                            sessions,
                            time_scope,
                            concurrency=concurrency,
                            window=window,
                            filter_only=filter_only,
                        )
        else:
            if not file_dfs:
                st.warning("请先上传 Excel 并完成列映射。")
            else:
                try:
                    sessions, _diag, window = _load_sessions_scoped(file_dfs, overrides)
                except Exception as e:
                    st.error(f"Excel 解析失败：{e}")
                    sessions = None

                if sessions is None:
                    pass
                elif not sessions:
                    if window:
                        st.error(f"窗口（{window['label']}）内没有符合发言时间的消息。")
                    else:
                        st.error("未解析出任何会话，请检查文件与列映射。")
                else:
                    _execute_qc_run(
                        cfg,
                        sessions,
                        time_scope,
                        concurrency=concurrency,
                        window=window,
                        filter_only=filter_only,
                    )

    # ---------- 成交客户心理学习 ----------
    st.subheader("成交客户学习")
    st.caption("每天北京时间 02:00 自动导入并分析。每位客户依据自己的聊天独立复盘；历史继承记录及新增聊天会重新分析。")
    if st.button("分析新增成交客户", type="secondary", key="analyze_deals_btn"):
        try:
            result = _run_deal_learning_with_progress(cfg, 20)
            st.success(
                f"成交学习完成：待分析 {result['total']} 个，"
                f"成功 {result['success']} 个（跳过 {result.get('skipped', 0)}，"
                f"增量 {result.get('incremental', 0)}，全量 {result.get('full', 0)}），"
                f"失败 {result['failed']} 个"
            )
            if result["failed"] > 0:
                st.warning("部分成交客户分析失败，请查看日志。")
                if result.get("errors"):
                    with st.expander("失败详情"):
                        for err in result["errors"]:
                            st.text(err)
        except RuntimeError as e:
            st.error(f"数据库不可用：{e}")
        except Exception as e:
            st.error(f"成交学习失败：{core.redact_secrets(str(e), cfg)}")

    # ---------- 删除客户流失复盘 ----------
    st.subheader("删除客户学习")
    st.caption("每天北京时间 02:00 自动拉取并入库：前一天有客户沟通、完整历史有效发言至少 7 条，排除黑粉；每位客户独立 AI 复盘。")
    if st.button("分析新增删除客户", type="secondary", key="analyze_deletes_btn"):
        try:
            result = _run_delete_learning_with_progress(cfg, 20)
            st.success(
                f"删除学习完成：待分析 {result['total']} 个，"
                f"成功 {result['success']} 个（跳过 {result.get('skipped', 0)}，"
                f"增量 {result.get('incremental', 0)}，全量 {result.get('full', 0)}），"
                f"失败 {result['failed']} 个"
            )
            churn_stats = result.get("churn_stats") or {}
            if churn_stats:
                st.caption("流失模式：" + " / ".join(f"{k} {v}" for k, v in churn_stats.items()))
            if result["failed"] > 0 and result.get("errors"):
                with st.expander("失败详情"):
                    for err in result["errors"]:
                        st.text(err)
        except RuntimeError as e:
            st.error(f"数据库不可用：{e}")
        except Exception as e:
            st.error(f"删除学习失败：{core.redact_secrets(str(e), cfg)}")

    # ---------- 统计与下载 ----------
    if st.session_state.get("last_filter_df") is not None:
        st.subheader("5. 筛选结果")
        filtered = st.session_state.last_filter_df
        total = st.session_state.last_filter_total
        _show_stat_row([("总会话数", total), ("符合条件", len(filtered)), ("已排除", total - len(filtered))])
        st.caption("客户发言时间为分析范围内最近一次有效发言时间；每通会话一行。")
        if filtered.empty:
            st.info("没有客户发言超过 3 句的会话，可下载仅包含表头的筛选表。")
        else:
            st.dataframe(filtered, use_container_width=True)

    if st.session_state.last_stats:
        st.subheader("5. 统计结果")
        s = st.session_state.last_stats
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("总会话数", s["总会话数"])
        m2.metric("跳过", s["跳过数量"])
        m3.metric("轻量质检", s["轻量质检数量"])
        m4.metric("完整质检", s["完整质检数量"])
        m5.metric("高风险", s["高风险数量"])

        st.caption("高风险 = 命中红线 / 判定不合格 / API 或 JSON 解析失败")

        t = st.session_state.get("last_timing")
        if t:
            st.markdown("#### 耗时统计")
            t1, t2, t3, t4 = st.columns(4)
            t1.metric("开始时间", t.get("开始时间", "—"))
            t2.metric("结束时间", t.get("结束时间", "—"))
            t3.metric("总耗时", t.get("总耗时文本", "—"))
            t4.metric("并发数", t.get("并发数", "—"))

            if t.get("分析客户数"):
                st.caption(
                    f"共分析 **{t['分析客户数']}** 通（轻量 + 完整），"
                    f"墙钟均价约 **{t.get('分析客户均价文本', '—')}**/通"
                )

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**轻量分析**")
                if t.get("轻量数量"):
                    st.write(
                        f"- 数量：**{t['轻量数量']}** 通\n"
                        f"- 单通实际耗时：**{t.get('轻量单通耗时文本', '—')}**（API 处理时间均值）\n"
                        f"- 并行折算：**{t.get('轻量并行折算文本', '—')}**/通"
                    )
                else:
                    st.write("本批无轻量分析。")
            with c2:
                st.markdown("**完整分析**")
                if t.get("完整数量"):
                    st.write(
                        f"- 数量：**{t['完整数量']}** 通\n"
                        f"- 单通实际耗时：**{t.get('完整单通耗时文本', '—')}**（API 处理时间均值）\n"
                        f"- 并行折算：**{t.get('完整并行折算文本', '—')}**/通"
                    )
                else:
                    st.write("本批无完整分析。")
            st.caption(
                "单通实际耗时 = 每通 API 调用的平均耗时；"
                "并行折算 = 按任务耗时占比分摊总墙钟时间后的每通均价（并发越高，折算越低）。"
            )

        dt = st.session_state.get("last_deep_timing")
        if dt:
            st.markdown("#### 深度复盘耗时")
            d1, d2, d3 = st.columns(3)
            d1.metric("深度复盘通数", dt.get("数量", 0))
            d2.metric("深度复盘总耗时", dt.get("总耗时文本", "—"))
            d3.metric("深度复盘均价", dt.get("单通均价文本", "—"))

    if st.session_state.get("last_quality_df") is not None:
        st.subheader("报告预览")
        tab_labels = [core.REPORT_SHEET_QUALITY, core.REPORT_SHEET_LOW_INTENT]
        df_deep = st.session_state.get("last_deep_df")
        if df_deep is not None and len(df_deep) > 0:
            tab_labels.append(core.REPORT_SHEET_DEEP_REVIEW)
        tabs = st.tabs(tab_labels)
        preview_cols = {
            core.REPORT_SHEET_QUALITY: st.session_state.last_quality_df,
            core.REPORT_SHEET_LOW_INTENT: st.session_state.last_low_df,
        }
        if core.REPORT_SHEET_DEEP_REVIEW in tab_labels:
            preview_cols[core.REPORT_SHEET_DEEP_REVIEW] = df_deep
        for tab, label in zip(tabs, tab_labels):
            with tab:
                df_show = preview_cols[label]
                hide = [c for c in ("原始对话",) if c in df_show.columns]
                st.dataframe(
                    df_show.drop(columns=hide, errors="ignore"),
                    use_container_width=True,
                )
                if hide:
                    with st.expander("查看原始对话"):
                        for _, r in df_show.iterrows():
                            st.markdown(f"**{r.get('联系人', '')}**")
                            st.text(str(r.get("原始对话", ""))[:8000])

    if st.session_state.report_bytes:
        st.subheader("6. 下载报告")
        counts = st.session_state.get("report_sheet_counts")
        scope_caption = f"分析范围：**{st.session_state.get('report_scope', '')}**"
        if st.session_state.get("last_window_label"):
            scope_caption += f"（{st.session_state.last_window_label}）"
        st.caption(scope_caption)
        if counts:
            sheet_desc = (
                f"「{core.REPORT_SHEET_QUALITY}」{counts[0]} 通 · "
                f"「{core.REPORT_SHEET_LOW_INTENT}」{counts[1]} 通"
            )
            if len(counts) > 2 and counts[2]:
                sheet_desc += f" · 「{core.REPORT_SHEET_DEEP_REVIEW}」{counts[2]} 通"
            st.caption(f"报告含工作表：{sheet_desc}")
        st.download_button(
            label="下载 Excel 客户筛选表" if st.session_state.get("report_kind") == "filter" else "下载 Excel 质检报告",
            data=st.session_state.report_bytes,
            file_name=st.session_state.report_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )


# ============================================================
# 上传成交客户标签页
# ============================================================
def _render_deal_import_settlement(summary: dict, target: str = "") -> None:
    """展示导入结算：总数、成功/跳过/失败及原因。"""
    settlement = summary.get("settlement") or {}
    st.subheader("导入结算")
    if target:
        st.caption(f"目标数据库：{target}")

    total = settlement.get("total_sessions", summary.get("session_count", 0))
    ok_n = settlement.get("success_count", 0)
    skip_n = settlement.get("skipped_count", 0)
    fail_n = settlement.get("failed_count", 0)

    _show_stat_row([
        ("上传文件", summary.get("file_count", 0)),
        ("解析会话", total),
        ("成功入库", ok_n),
        ("跳过", skip_n),
        ("失败", fail_n),
        ("标记已成交", summary.get("marked_deal", 0)),
    ])
    _show_stat_row([
        ("新增客户", summary.get("customers_created", 0)),
        ("新增消息", summary.get("messages_inserted", 0)),
        ("无新增聊天", summary.get("unchanged_sessions", 0)),
    ])

    skip_reasons = settlement.get("skip_by_reason") or {}
    fail_reasons = settlement.get("fail_by_reason") or {}

    if skip_reasons or fail_reasons:
        st.markdown("**未全部成功的原因统计**")
        reason_rows = []
        for reason, cnt in skip_reasons.items():
            reason_rows.append({"类型": "跳过", "原因": reason, "数量": cnt})
        for reason, cnt in fail_reasons.items():
            reason_rows.append({"类型": "失败", "原因": reason, "数量": cnt})
        if reason_rows:
            _show_table(pd.DataFrame(reason_rows))

    file_stats = summary.get("file_stats") or []
    if file_stats:
        with st.expander("按文件查看", expanded=False):
            _show_table(pd.DataFrame(file_stats))

    failed_items = settlement.get("failed_items") or []
    if failed_items:
        with st.expander(f"失败明细（{len(failed_items)} 条）", expanded=True):
            _show_table(pd.DataFrame([{
                "会话ID": x.get("session_id"),
                "联系人": x.get("contact_name"),
                "来源文件": x.get("source_file"),
                "原因": x.get("reason"),
            } for x in failed_items]))

    skipped_items = settlement.get("skipped_items") or []
    if skipped_items:
        with st.expander(f"跳过明细（{len(skipped_items)} 条）", expanded=fail_n == 0):
            _show_table(pd.DataFrame([{
                "会话ID": x.get("session_id"),
                "联系人": x.get("contact_name"),
                "来源文件": x.get("source_file"),
                "原因": x.get("reason"),
            } for x in skipped_items]))

    success_items = settlement.get("success_items") or []
    if success_items and ok_n <= 30:
        with st.expander(f"成功明细（{len(success_items)} 条）", expanded=False):
            _show_table(pd.DataFrame([{
                "会话ID": x.get("session_id"),
                "联系人": x.get("contact_name"),
                "新增消息": x.get("messages_inserted", 0),
                "新客户": "是" if x.get("is_new_customer") else "否",
            } for x in success_items]))
    elif success_items:
        st.caption(f"成功入库 {ok_n} 通会话（明细过多，仅展示统计）")

    if summary.get("errors"):
        with st.expander("其他警告"):
            for err in summary["errors"][:30]:
                st.text(err)


def _render_deal_learning_settlement(learn: dict) -> None:
    """心理学习结算（按客户）。"""
    st.subheader("学习结算")
    st.caption("两阶段：先比对已有案例 → 跳过 / 增量 / 全量学习。")
    _show_stat_row([
        ("待分析客户", learn.get("total", 0)),
        ("成功", learn.get("success", 0)),
        ("继承跳过", learn.get("skipped", 0)),
        ("增量学习", learn.get("incremental", 0)),
        ("全量复盘", learn.get("full", 0)),
        ("失败", learn.get("failed", 0)),
    ])

    if learn.get("errors"):
        with st.expander(f"失败原因（{len(learn['errors'])} 条）", expanded=True):
            for err in learn["errors"][:30]:
                st.text(err)

    if learn.get("learned_items"):
        with st.expander("智能体判断规则（摘要）", expanded=False):
            for item in learn["learned_items"][:15]:
                mode = item.get("learn_mode") or ""
                title = f"**{item.get('contact_name')}**"
                if mode:
                    title += f" · {mode}"
                st.markdown(title)
                st.text((item.get("recommended_agent_rules") or "")[:300])


def _run_deal_learning_with_progress(cfg, limit: int):
    """成交心理学习，带 Streamlit 进度条。"""
    from deal_import_core import run_deal_learning

    progress = st.progress(0, text="心理学习准备中…")
    status = st.empty()
    logs: list[str] = []

    def on_progress(done, total, name, ok, mode=""):
        pct = done / total if total else 1.0
        tag = "✓" if ok else "✗"
        mode_label = {
            "skipped": "跳过",
            "incremental": "增量",
            "full": "全量",
            "failed": "失败",
        }.get(mode, "")
        suffix = f" [{mode_label}]" if mode_label else ""
        line = f"[{done}/{total}] {tag} 客户 {name}{suffix}"
        logs.append(line)
        progress.progress(pct, text=f"心理学习（按客户）{done}/{total}")
        tail = logs[-6:]
        status.markdown("**最近进度**\n\n" + "\n\n".join(f"- {l}" for l in tail))

    result = run_deal_learning(int(limit), qc_cfg=cfg, on_progress=on_progress)
    progress.progress(1.0, text="心理学习完成")
    return result


def _render_deal_learn_prompt(cfg, default_limit: int) -> None:
    """导入结算后询问是否开始按客户做心理学习。"""
    from db import count_unanalyzed_deal_customers

    pending = count_unanalyzed_deal_customers()
    st.subheader("5. 是否开始心理学习？")
    st.markdown(
        "分析单位是 **每位成交客户**（使用该客户**全部聊天记录**做一次复盘），"
        "不是按 Excel 行或单段对话逐条分析；聊天未变化且已独立分析的客户不会重复分析；历史继承记录会补跑。"
    )

    if pending <= 0:
        st.success("当前所有成交客户均已完成心理学习，无需再分析。")
        return

    st.info(f"当前有 **{pending}** 位成交客户尚未学习。")
    suggest = min(pending, default_limit)
    analyze_limit = st.number_input(
        "本次最多分析客户数",
        min_value=1,
        max_value=max(1, pending),
        value=suggest,
        key="deal_import_analyze_limit",
    )

    c1, c2 = st.columns(2)
    with c1:
        start = st.button("开始心理学习", type="primary", key="deal_import_learn_start")
    with c2:
        skip = st.button("暂不分析", key="deal_import_learn_skip")

    if skip:
        st.session_state.deal_learn_skipped = True
        st.caption("已跳过；可稍后在「聊天质检」页点击「分析新增成交客户」，或等凌晨自动任务。")

    if start:
        try:
            learn = _run_deal_learning_with_progress(cfg, int(analyze_limit))
        except RuntimeError as e:
            st.error(f"数据库不可用：{e}")
            return
        except Exception as e:
            st.error(f"心理学习失败：{core.redact_secrets(str(e), cfg)}")
            st.caption("已成功分析的客户结果已写入数据库；可稍后减少「本次最多分析客户数」继续跑剩余客户。")
            return

        st.session_state.deal_last_learn_result = learn
        st.session_state.deal_learn_skipped = False
        st.success("心理学习完成，请查看下方结算。")
        _render_deal_learning_settlement(learn)

    elif st.session_state.get("deal_last_learn_result"):
            _render_deal_learning_settlement(st.session_state.deal_last_learn_result)


def _kb_clear_refine_state() -> None:
    st.session_state.kb_refine_result = None
    st.session_state.kb_refined_rules = None


def _kb_get_write_rules(extract_fn) -> tuple[list[str], str]:
    """写入步骤使用的规则列表及来源说明。"""
    refined = st.session_state.get("kb_refined_rules")
    if refined is not None:
        return refined, f"AI 提纯勾选（{len(refined)} 条）"
    raw = extract_fn(st.session_state.get("kb_review_md") or "")
    return raw, f"编辑区原始（{len(raw)} 条）"


def _kb_prompts_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def _kb_supplement_path(kb_type: str) -> str:
    name = (
        "delete_learned_supplement.md"
        if kb_type == "delete"
        else "deal_learned_supplement.md"
    )
    return os.path.join(_kb_prompts_dir(), name)


def _kb_read_supplement_file(kb_type: str) -> str:
    path = _kb_supplement_path(kb_type)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return ""


def _kb_export_rules_markdown(days: int, kb_type: str):
    from weekly_prompt_merge import export_rules_markdown

    try:
        return export_rules_markdown(days, rule_type=kb_type)
    except TypeError:
        if kb_type == "delete":
            raise RuntimeError(
                "当前 weekly_prompt_merge.py 过旧，不支持删除规则导出，请 git pull 后重启服务"
            ) from None
        return export_rules_markdown(days)


def _kb_build_supplement_markdown(rules: list[str], kb_type: str) -> str:
    from weekly_prompt_merge import build_supplement_markdown

    try:
        return build_supplement_markdown(rules, rule_type=kb_type)
    except TypeError:
        return build_supplement_markdown(rules)


def _kb_apply_rules_to_supplement(
    rules: list[str],
    *,
    merge_existing: bool,
    kb_type: str,
) -> dict:
    from weekly_prompt_merge import apply_rules_to_supplement

    try:
        return apply_rules_to_supplement(
            rules, merge_existing=merge_existing, rule_type=kb_type
        )
    except TypeError:
        if kb_type == "delete":
            raise RuntimeError(
                "当前 weekly_prompt_merge.py 过旧，不支持写入删除规则库，请 git pull 后重启服务"
            ) from None
        return apply_rules_to_supplement(rules, merge_existing=merge_existing)


def _render_database_connection(key: str) -> None:
    from db import mysql_connection_info, test_mysql_connection

    info = mysql_connection_info()
    target = f"{info['host'] or '未配置主机'}:{info['port']}/{info['database'] or '未配置库名'}"
    with st.expander("数据库连接检查与配置", expanded=False):
        st.write(f"当前连接目标：**{target}**")
        st.caption(f"读取配置：{info['env_path']}（{'文件存在' if info['env_exists'] else '文件不存在'}）；进程 MYSQL_* 环境变量优先。")
        if info["process_overrides"]:
            st.caption("进程环境变量覆盖项：" + "、".join(info["process_overrides"]))
        if info["host"] in ("127.0.0.1", "localhost", "::1"):
            st.caption("Docker 中的 127.0.0.1 指向当前网页容器。若 MySQL 在其他容器或宿主机，请填写网页容器可访问的数据库地址。")
        if st.button("测试数据库连接 / 重新连接", key=f"{key}_db_test"):
            with st.spinner("正在测试数据库连接…"):
                try:
                    test_mysql_connection()
                    st.success("MySQL 连接成功，可以重试导出或导入。")
                except RuntimeError as exc:
                    st.error(str(exc))


def _render_knowledge_base_tab(cfg) -> None:
    """全局知识库：从 DB 导出规则 → 在线审核 → AI 提纯 → 写入 supplement。"""
    from rule_refine import (
        collect_rules_from_categories,
        refine_rules,
        refine_stats,
    )
    from weekly_prompt_merge import extract_rules_from_text

    rule_type = st.radio(
        "知识库类型",
        ["成交复盘规则", "删除复盘规则"],
        horizontal=True,
        key="kb_rule_type",
    )
    kb_type = "delete" if rule_type.startswith("删除") else "deal"
    supplement_path = _kb_supplement_path(kb_type)
    table_name = "delete_analysis" if kb_type == "delete" else "deal_analysis"

    st.caption(
        f"将{rule_type}审核后写入全局知识库；质检 full/lite 模式会自动加载对应 supplement 文件。"
    )

    st.subheader("1. 当前全局知识库")
    current = _kb_read_supplement_file(kb_type)
    if current.strip():
        parsed_current = extract_rules_from_text(current)
        st.caption(f"路径：`{supplement_path}` · 共 **{len(parsed_current)}** 条规则")
        with st.expander(f"查看 {os.path.basename(supplement_path)}", expanded=False):
            st.markdown(current[:16000])
            if len(current) > 16000:
                st.caption("（内容过长，仅展示前 16000 字）")
        if st.button("载入到编辑器（基于当前知识库）", key="kb_load_current"):
            st.session_state.kb_review_md = current
            st.session_state.kb_parsed_preview = None
            st.session_state.kb_supplement_preview = None
            _kb_clear_refine_state()
            st.rerun()
    else:
        st.info("暂无全局知识库文件；完成下方「导出 → 审核 → 写入」后将自动创建。")

    st.subheader("2. 从数据库导出待审核规则")
    _render_database_connection("kb")
    st.markdown(
        f"从 MySQL `{table_name}` 读取最近 N 天内**不重复**的「智能体判断规则」，"
        "生成待审核 Markdown。"
    )
    c1, c2 = st.columns([1, 2])
    with c1:
        days = st.number_input("最近 N 天", min_value=1, max_value=365, value=7, key="kb_export_days")
    with c2:
        st.caption("导出后可在下方编辑器删除不适用的 `### 规则` 块。")

    if st.button("从数据库导出到编辑器", type="secondary", key="kb_export_btn"):
        try:
            rules, md = _kb_export_rules_markdown(int(days), kb_type)
            st.session_state.kb_review_md = md
            st.session_state.kb_parsed_preview = None
            st.session_state.kb_supplement_preview = None
            _kb_clear_refine_state()
            st.success(f"已加载 **{len(rules)}** 条不重复规则（最近 {int(days)} 天）")
            st.rerun()
        except Exception as e:
            from db import format_database_error
            st.error(f"导出失败：{format_database_error(e)}")

    st.subheader("3. 审核与编辑")
    st.text_area(
        "待审核 Markdown",
        height=360,
        key="kb_review_md",
        placeholder="点击「从数据库导出」或「载入当前知识库」后在此编辑…",
    )

    c3, c4 = st.columns(2)
    with c3:
        if st.button("预览将写入的规则条数", key="kb_preview_parse"):
            parsed = extract_rules_from_text(st.session_state.get("kb_review_md") or "")
            st.session_state.kb_parsed_preview = parsed
            st.session_state.kb_supplement_preview = None
    with c4:
        st.download_button(
            label="下载当前编辑内容为 .md",
            data=(st.session_state.get("kb_review_md") or "").encode("utf-8"),
            file_name=f"待审核{'删除' if kb_type == 'delete' else '成交'}规则-{datetime.now().strftime('%Y%m%d%H%M%S')}.md",
            mime="text/markdown",
            key="kb_download_md",
        )

    parsed = st.session_state.get("kb_parsed_preview")
    if parsed is not None:
        if parsed:
            st.success(f"解析到 **{len(parsed)}** 条规则")
            with st.expander("规则摘要预览", expanded=False):
                for i, rule in enumerate(parsed[:20], 1):
                    st.markdown(f"**规则 {i}**")
                    st.text(rule[:500] + ("…" if len(rule) > 500 else ""))
                if len(parsed) > 20:
                    st.caption(f"另有 {len(parsed) - 20} 条未展示")
        else:
            st.warning("未能从编辑区解析出规则，请保留 `### 规则 N` 与「智能体判断规则」块格式。")

    st.subheader("4. AI 提纯去重与分类")
    st.caption(
        "调用 API 合并语义重复的规则，并按场景自动分类。"
        "可取消勾选「投资类」「情感操控类」等不需要写入的类别。"
    )

    c_ref1, c_ref2 = st.columns([1, 2])
    with c_ref1:
        if st.button("开始 AI 提纯去重", type="secondary", key="kb_refine_btn"):
            src_rules = extract_rules_from_text(st.session_state.get("kb_review_md") or "")
            if not src_rules:
                st.error("编辑区没有可提纯的规则，请先导出或解析。")
            else:
                with st.spinner(f"正在对 {len(src_rules)} 条规则提纯去重，请稍候…"):
                    refine_result = refine_rules(src_rules, cfg)
                if refine_result.get("ok"):
                    st.session_state.kb_refine_result = refine_result
                    st.session_state.kb_refined_rules = None
                    st.session_state.kb_supplement_preview = None
                    st.rerun()
                else:
                    st.error(refine_result.get("error") or "提纯失败")
    with c_ref2:
        if st.session_state.get("kb_refine_result"):
            if st.button("清除提纯结果", key="kb_refine_clear"):
                _kb_clear_refine_state()
                st.session_state.kb_supplement_preview = None
                st.rerun()

    refine_result = st.session_state.get("kb_refine_result")
    if refine_result and refine_result.get("ok"):
        if refine_result.get("汇总说明"):
            st.info(refine_result["汇总说明"])
        st.caption(
            f"原始 **{refine_result.get('原始条数', 0)}** 条 → "
            f"去重后 **{refine_result.get('去重后条数', 0)}** 条 · "
            f"共 **{len(refine_result.get('分类列表') or [])}** 个分类"
        )

        with st.form("kb_refine_category_form", clear_on_submit=False):
            st.markdown("**勾选要写入知识库的分类**（未勾选的分类下所有规则均不写入）")
            enabled_ids: list[str] = []
            for cat in refine_result.get("分类列表") or []:
                cid = str(cat.get("分类ID") or "")
                title = cat.get("分类标题") or cid
                desc = (cat.get("分类说明") or "").strip()
                n_rules = len(cat.get("规则条目") or [])
                default_on = bool(cat.get("默认勾选", True))
                label = f"{title}（{n_rules} 条）"
                if desc:
                    label += f" — {desc[:80]}{'…' if len(desc) > 80 else ''}"
                if not default_on:
                    label += " · 建议排除"
                if st.checkbox(label, value=default_on, key=f"kb_refine_cat_{cid}"):
                    enabled_ids.append(cid)

            live_stats = refine_stats(refine_result, set(enabled_ids))
            st.caption(
                f"当前勾选：**{live_stats['已勾选分类数']}/{live_stats['分类数']}** 类，"
                f"将写入 **{live_stats['将写入条数']}** 条规则"
            )
            submitted = st.form_submit_button("确认选用以上勾选结果", type="primary")
            if submitted:
                selected = collect_rules_from_categories(refine_result, enabled_ids)
                if not selected:
                    st.warning("请至少勾选一个分类。")
                else:
                    st.session_state.kb_refined_rules = selected
                    st.session_state.kb_supplement_preview = None
                    st.rerun()

        for cat in refine_result.get("分类列表") or []:
            title = cat.get("分类标题") or cat.get("分类ID")
            n_rules = len(cat.get("规则条目") or [])
            with st.expander(f"查看「{title}」规则详情（{n_rules} 条）", expanded=False):
                for item in cat.get("规则条目") or []:
                    src = item.get("合并自序号") or []
                    src_hint = (
                        f"合并自原始规则 #{', #'.join(str(x) for x in src)}"
                        if src else ""
                    )
                    st.markdown(f"**{item.get('规则ID', '')}** {src_hint}")
                    st.text(item.get("规则文本") or "")

    if st.session_state.get("kb_refined_rules") is not None:
        n = len(st.session_state.kb_refined_rules)
        st.success(f"✓ 已确认选用 **{n}** 条提纯规则用于写入（跳过步骤 4 则使用编辑区原始规则）")

    st.subheader("5. 写入全局知识库")
    write_rules, write_source = _kb_get_write_rules(extract_rules_from_text)
    st.caption(f"当前写入来源：**{write_source}**")

    merge_existing = st.checkbox(
        "合并到现有知识库（追加不重复项，保留已有规则）",
        value=False,
        key="kb_merge_existing",
    )
    if not merge_existing:
        st.caption(f"未勾选时将**覆盖**写入 `{os.path.basename(supplement_path)}`。")

    c5, c6 = st.columns(2)
    with c5:
        if st.button("仅预览合并结果", key="kb_dry_apply"):
            rules = write_rules
            if not rules:
                st.warning("没有可预览的规则")
            else:
                preview_rules = list(rules)
                if merge_existing:
                    existing = extract_rules_from_text(_kb_read_supplement_file(kb_type))
                    seen = set(existing)
                    preview_rules = list(existing)
                    for r in rules:
                        if r not in seen:
                            preview_rules.append(r)
                            seen.add(r)
                st.session_state.kb_supplement_preview = _kb_build_supplement_markdown(
                    preview_rules, kb_type
                )

    preview_md = st.session_state.get("kb_supplement_preview")
    if preview_md:
        with st.expander("合并结果预览（未写入文件）", expanded=True):
            st.markdown(preview_md[:12000])

    with c6:
        if st.button("确认写入全局知识库", type="primary", key="kb_apply"):
            rules = write_rules
            if not rules:
                st.error("没有可写入的规则。请先导出、提纯选用，或保留编辑区至少一条规则。")
            else:
                try:
                    result = _kb_apply_rules_to_supplement(
                        rules, merge_existing=merge_existing, kb_type=kb_type
                    )
                except Exception as e:
                    st.error(f"写入失败：{core.redact_secrets(str(e), cfg)}")
                else:
                    if result.get("ok"):
                        st.success(
                            f"已写入 **{result['count']}** 条规则 → `{result['path']}`"
                        )
                        st.session_state.kb_supplement_preview = result.get("preview")
                        st.caption("质检时 full/lite 模式将自动加载该文件，无需重启。")
                    else:
                        st.error(result.get("error") or "写入失败")


def _render_delete_learning_settlement(learn: dict) -> None:
    st.subheader("学习结算")
    st.caption("两阶段：先比对已有删除案例 → 跳过 / 增量 / 全量复盘（含成交对比注入）。")
    _show_stat_row([
        ("待分析客户", learn.get("total", 0)),
        ("成功", learn.get("success", 0)),
        ("继承跳过", learn.get("skipped", 0)),
        ("增量学习", learn.get("incremental", 0)),
        ("全量复盘", learn.get("full", 0)),
        ("失败", learn.get("failed", 0)),
    ])
    churn_stats = learn.get("churn_stats") or {}
    if churn_stats:
        st.caption("流失模式：" + " / ".join(f"{k} {v}" for k, v in churn_stats.items()))
    if learn.get("errors"):
        with st.expander(f"失败原因（{len(learn['errors'])} 条）", expanded=True):
            for err in learn["errors"][:30]:
                st.text(err)
    if learn.get("learned_items"):
        with st.expander("智能体判断规则（摘要）", expanded=False):
            for item in learn["learned_items"][:15]:
                mode = item.get("learn_mode") or ""
                title = f"**{item.get('contact_name')}**"
                if mode:
                    title += f" · {mode}"
                if item.get("churn_pattern"):
                    title += f" · {item['churn_pattern']}"
                st.markdown(title)
                st.text((item.get("recommended_agent_rules") or "")[:300])


def _run_delete_learning_with_progress(cfg, limit: int):
    from delete_import_core import run_delete_learning

    progress = st.progress(0, text="删除客户流失复盘准备中…")
    status = st.empty()
    logs: list[str] = []

    def on_progress(done, total, name, ok, mode=""):
        pct = done / total if total else 1.0
        tag = "✓" if ok else "✗"
        mode_label = {
            "skipped": "跳过",
            "incremental": "增量",
            "full": "全量",
            "failed": "失败",
        }.get(mode, "")
        suffix = f" [{mode_label}]" if mode_label else ""
        line = f"[{done}/{total}] {tag} 客户 {name}{suffix}"
        logs.append(line)
        progress.progress(pct, text=f"删除复盘（按客户）{done}/{total}")
        tail = logs[-6:]
        status.markdown("**最近进度**\n\n" + "\n\n".join(f"- {l}" for l in tail))

    result = run_delete_learning(int(limit), qc_cfg=cfg, on_progress=on_progress)
    progress.progress(1.0, text="删除复盘完成")
    return result


def _render_delete_learn_prompt(cfg, default_limit: int) -> None:
    from db import count_unanalyzed_delete_customers

    pending = count_unanalyzed_delete_customers()
    st.subheader("5. 是否开始流失复盘？")
    st.markdown(
        "分析单位是 **每位删除客户**（使用该客户**全部聊天记录**做一次复盘），"
        "聊天未变化且已独立分析的客户不会重复分析；历史继承记录会补跑。"
    )
    if pending <= 0:
        st.success("当前所有删除客户均已完成流失复盘，无需再分析。")
        return
    st.info(f"当前有 **{pending}** 位删除客户尚未学习。")
    suggest = min(pending, default_limit)
    analyze_limit = st.number_input(
        "本次最多分析客户数",
        min_value=1,
        max_value=max(1, pending),
        value=suggest,
        key="delete_import_analyze_limit",
    )
    c1, c2 = st.columns(2)
    with c1:
        start = st.button("开始流失复盘", type="primary", key="delete_import_learn_start")
    with c2:
        skip = st.button("暂不分析", key="delete_import_learn_skip")
    if skip:
        st.session_state.delete_learn_skipped = True
        st.caption("已跳过；可稍后在「聊天质检」页点击「分析新增删除客户」，或等凌晨自动任务。")
    if start:
        try:
            learn = _run_delete_learning_with_progress(cfg, int(analyze_limit))
        except RuntimeError as e:
            st.error(f"数据库不可用：{e}")
            return
        except Exception as e:
            st.error(f"流失复盘失败：{core.redact_secrets(str(e), cfg)}")
            return
        st.session_state.delete_last_learn_result = learn
        st.session_state.delete_learn_skipped = False
        st.success("流失复盘完成，请查看下方结算。")
        _render_delete_learning_settlement(learn)
    elif st.session_state.get("delete_last_learn_result"):
        _render_delete_learning_settlement(st.session_state.delete_last_learn_result)


def _render_delete_import_settlement(summary: dict, target: str = "") -> None:
    settlement = summary.get("settlement") or {}
    st.subheader("导入结算")
    if target:
        st.caption(f"目标数据库：{target}")
    _show_stat_row([
        ("上传文件", summary.get("file_count", 0)),
        ("解析会话", settlement.get("total_sessions", summary.get("session_count", 0))),
        ("成功入库", settlement.get("success_count", 0)),
        ("跳过", settlement.get("skipped_count", 0)),
        ("失败", settlement.get("failed_count", 0)),
        ("标记已删除", summary.get("marked_deleted", 0)),
    ])
    if summary.get("errors"):
        with st.expander("其他警告"):
            for err in summary["errors"][:30]:
                st.text(err)


def _render_delete_import_tab(cfg):
    from deal_import_core import get_mysql_target_label, load_import_config
    from delete_import_core import execute_delete_import

    import_cfg = load_import_config()
    target = get_mysql_target_label(import_cfg)
    default_limit = int(import_cfg.get("default_analyze_limit") or 20)
    sm_cfg = import_cfg.get("salesmartly") or {}
    delete_keywords = sm_cfg.get("delete_tag_keywords") or ["删", "删除", "拉黑", "拒收"]

    st.caption(
        "将**已删除**客户聊天写入 MySQL 并**直接标记为已删除**（排除黑粉，不跑质检）。"
        "每天北京时间 02:00 自动从 SaleSmartly 拉取、入库并分析，也可手动上传 Excel。"
    )
    st.info(f"导入目标数据库：**{target}**")
    _render_database_connection("delete_import")

    source = st.radio(
        "数据来源",
        ["SaleSmartly API（按沟通时间筛选）", "上传 Excel"],
        horizontal=True,
        key="delete_import_source",
    )

    if source.startswith("SaleSmartly"):
        st.subheader("1. 从 SaleSmartly 拉取")
        st.markdown(
            f"规则：先拉取含 **{' / '.join(delete_keywords)}** 的删除标签客户，**排除黑粉**，"
            "再拉聊天记录，保留选定时间范围内**有客户有效沟通**，且完整历史客户有效发言 **超过 6 条（至少 7 条）** 的会话。"
            "符合条件的客户导入完整聊天，供后续流失复盘；不依赖昨天或日期标签。"
        )
        today = datetime.now().date()
        date_start_col, date_end_col = st.columns(2)
        with date_start_col:
            start_date = st.date_input("沟通开始日期", value=today - timedelta(days=1), key="delete_window_start_date")
            start_time = st.time_input("沟通开始时间", value=datetime.min.time(), key="delete_window_start_time")
        with date_end_col:
            end_date = st.date_input("沟通结束日期", value=today, key="delete_window_end_date")
            end_time = st.time_input("沟通结束时间（不含）", value=datetime.min.time(), key="delete_window_end_time")
        window_start = datetime.combine(start_date, start_time)
        window_end = datetime.combine(end_date, end_time)
        valid_window = window_start < window_end
        st.caption("范围包含开始时刻，不包含结束时刻。沿用有效发言计数规则，图片和单独的“1”不计入。")
        if not valid_window:
            st.warning("结束时间必须晚于开始时间。")
        col_a, col_b = st.columns(2)
        with col_a:
            preview_api = st.button("预览范围内删除客户（不写库）", key="delete_api_preview", disabled=not valid_window)
        with col_b:
            import_api = st.button("拉取范围内删除客户并导入", type="primary", key="delete_api_import", disabled=not valid_window)

        if preview_api or import_api:
            from fetch_cancel import FetchCancelledError
            from fetch_delete_daily import run_fetch_delete_daily

            on_fetch_progress, fetch_bar, cancel_check = _begin_salesmartly_fetch()
            api_result = None
            try:
                api_result = run_fetch_delete_daily(
                    dry_run=preview_api,
                    window_start=window_start,
                    window_end=window_end,
                    on_progress=on_fetch_progress,
                    cancel_check=cancel_check,
                )
                fetch_bar.progress(1.0, text="拉取完成")
            except FetchCancelledError:
                st.warning("拉取已取消。")
            except Exception as e:
                st.error(f"拉取失败：{core.redact_secrets(str(e), cfg)}")
                api_result = None

            if api_result is not None:
                meta = api_result.get("meta") or {}
                st.caption(
                    f"沟通时间：**{api_result.get('window_label')}** · 删除标签客户 **{meta.get('delete_contacts', 0)}** 人 · "
                    f"符合条件 **{meta.get('sessions_with_messages', 0)}** 通 · "
                    f"无客户沟通排除 **{meta.get('skipped_no_customer_speech', 0)}** 通 · "
                    f"历史不足 7 条排除 **{meta.get('skipped_insufficient_speech', 0)}** 通"
                )
                if api_result.get("error") and not api_result.get("ok"):
                    st.error(api_result["error"])
                elif preview_api:
                    st.success("预览完成（未写入数据库）。")
                    if api_result.get("preview"):
                        st.dataframe(pd.DataFrame(api_result["preview"]), use_container_width=True)
                    else:
                        st.info("所选范围内没有符合条件的删除客户。")
                elif api_result.get("ok"):
                    summary = api_result.get("summary") or {}
                    st.session_state.delete_last_import_summary = summary
                    st.session_state.delete_last_import_target = api_result.get("target") or target
                    st.session_state.delete_learn_skipped = False
                    st.session_state.delete_last_learn_result = None
                    st.success("导入完成，请查看下方结算。")
    else:
        uploaded = st.file_uploader(
            "选择已删除客户聊天记录（可多选 .xlsx）",
            type=["xlsx"],
            accept_multiple_files=True,
            key="delete_import_upload",
        )
        if uploaded:
            try:
                file_dfs, read_errors = _read_uploaded_files(uploaded)
            except ValueError as e:
                st.error(str(e))
                file_dfs = None
            if file_dfs:
                for err in read_errors:
                    st.warning(err)
                if st.button("开始导入", type="primary", key="delete_import_start"):
                    with st.spinner("正在导入…"):
                        try:
                            result = execute_delete_import(
                                file_dfs=file_dfs,
                                run_analyze=False,
                                qc_cfg=cfg,
                            )
                        except RuntimeError as e:
                            st.error(f"数据库连接失败：{e}")
                            return
                        except Exception as e:
                            st.error(f"导入失败：{core.redact_secrets(str(e), cfg)}")
                            return
                    if not result.get("ok"):
                        st.error(result.get("error") or "导入失败")
                        return
                    st.session_state.delete_last_import_summary = result.get("summary") or {}
                    st.session_state.delete_last_import_target = result.get("target") or target
                    st.session_state.delete_learn_skipped = False
                    st.session_state.delete_last_learn_result = None
                    st.success("导入完成，请查看下方结算。")

    if st.session_state.get("delete_last_import_summary"):
        st.divider()
        _render_delete_import_settlement(
            st.session_state.delete_last_import_summary,
            st.session_state.get("delete_last_import_target") or target,
        )
        if st.session_state.get("delete_learn_skipped"):
            if st.button("现在开始做流失复盘", key="delete_learn_unskip"):
                st.session_state.delete_learn_skipped = False
                st.rerun()
        elif not st.session_state.get("delete_last_learn_result"):
            _render_delete_learn_prompt(cfg, default_limit)
        else:
            _render_delete_learning_settlement(st.session_state.delete_last_learn_result)


def _render_deal_import_tab(cfg):
    from deal_import_core import execute_deal_import, get_mysql_target_label, load_import_config

    import_cfg = load_import_config()
    target = get_mysql_target_label(import_cfg)
    default_limit = int(import_cfg.get("default_analyze_limit") or 20)
    sm_cfg = import_cfg.get("salesmartly") or {}
    deal_keywords = sm_cfg.get("deal_tag_keywords") or ["全款", "定金", "分期"]

    st.caption(
        "将**已成交**客户聊天写入 MySQL 并**直接标记为已成交**（不跑质检）。"
        "每天北京时间 02:00 自动从 SaleSmartly 拉取、入库并分析，也可手动上传 Excel。"
        "导入完成后可在下方启动成交心理学习。"
    )
    st.info(f"导入目标数据库：**{target}**（账号密码在服务器 `.env` 中配置）")

    source = st.radio(
        "数据来源",
        ["SaleSmartly API（昨日成交）", "上传 Excel"],
        horizontal=True,
        key="deal_import_source",
    )

    if source.startswith("SaleSmartly"):
        st.subheader("1. 从 SaleSmartly 拉取")
        st.markdown(
            f"规则：昨天日期标签下的客户，且访客标签含 **{' / '.join(deal_keywords)}**。"
            " 每天**北京时间 02:00**自动拉取、入库，并逐位执行 AI 心理分析；也可在下方手动导入。"
        )
        col_a, col_b = st.columns(2)
        with col_a:
            preview_api = st.button("预览昨日成交（不写库）", key="deal_api_preview")
        with col_b:
            import_api = st.button("拉取昨日成交并导入", type="primary", key="deal_api_import")

        if preview_api or import_api:
            from fetch_cancel import FetchCancelledError
            from fetch_deal_daily import run_fetch_deal_daily

            on_fetch_progress, fetch_bar, cancel_check = _begin_salesmartly_fetch()
            api_result = None
            try:
                api_result = run_fetch_deal_daily(
                    dry_run=preview_api,
                    on_progress=on_fetch_progress,
                    cancel_check=cancel_check,
                )
                fetch_bar.progress(1.0, text="拉取完成")
            except FetchCancelledError:
                st.warning("拉取已取消（页面已关闭或连接已断开）。")
            except Exception as e:
                st.error(f"拉取失败：{core.redact_secrets(str(e), cfg)}")
                api_result = None

            if api_result is not None:
                meta = api_result.get("meta") or {}
                st.caption(
                    f"业务日 **{api_result.get('target_day')}** · 日期标签 **{meta.get('date_tag', '')}** · "
                    f"成交 **{meta.get('deal_contacts', 0)}** 人 · "
                    f"有效会话 **{meta.get('sessions_with_messages', 0)}**"
                )
                if api_result.get("error") and not api_result.get("ok"):
                    st.error(api_result["error"])
                elif preview_api:
                    if meta.get("deal_contacts", 0) == 0:
                        st.warning("昨日没有符合条件的成交客户。")
                    else:
                        st.success("预览完成（未写入数据库）。")
                elif api_result.get("ok"):
                    summary = api_result.get("summary") or {}
                    st.session_state.deal_last_import_summary = summary
                    st.session_state.deal_last_import_target = api_result.get("target") or target
                    st.session_state.deal_learn_skipped = False
                    st.session_state.deal_last_learn_result = None
                    st.success("导入完成，请查看下方结算。")

    else:
        st.subheader("1. 上传 Excel")
        uploaded = st.file_uploader(
            "选择已成交客户聊天记录（可多选 .xlsx）",
            type=["xlsx"],
            accept_multiple_files=True,
            key="deal_import_upload",
        )
        if not uploaded:
            st.markdown(
                "表头示例：**会话ID**、**联系人**、**接待成员**、**社媒渠道**、**会话消息**"
                "（这五列会自动识别）。"
            )
        else:
            try:
                file_dfs, read_errors = _read_uploaded_files(uploaded)
            except ValueError as e:
                st.error(str(e))
                file_dfs = None

            if file_dfs:
                for err in read_errors:
                    st.warning(err)

                preview_name, preview_df = file_dfs[0]
                st.subheader("2. 数据预览")
                with st.expander(
                    f"展开查看（{preview_name} · 共 {len(preview_df)} 行 · 已选 {len(file_dfs)} 个文件）",
                    expanded=False,
                ):
                    _show_table(preview_df.head(20))

                st.subheader("3. 列名映射")
                all_columns = list(preview_df.columns)
                deal_overrides_key = "deal_column_overrides"
                if deal_overrides_key not in st.session_state or not st.session_state[deal_overrides_key]:
                    st.session_state[deal_overrides_key], opts, auto_det = _default_override_for_cols(all_columns)
                else:
                    opts = core.suggest_column_options(all_columns)
                    auto_det = core.detect_columns(all_columns)

                with st.expander("展开调整列名映射（「消息内容」为必填）", expanded=False):
                    cols_ui = st.columns(4)
                    overrides = {}
                    for i, (key, label) in enumerate(COLUMN_FIELDS):
                        with cols_ui[i % 4]:
                            current = st.session_state[deal_overrides_key].get(key, "（不映射）")
                            if current not in opts:
                                current = "（不映射）"
                            overrides[key] = st.selectbox(
                                label,
                                opts,
                                index=opts.index(current) if current in opts else 0,
                                key=f"deal_colmap_{key}",
                            )
                    st.session_state[deal_overrides_key] = overrides

                column_map = _build_column_map(all_columns, st.session_state[deal_overrides_key])
                if not column_map.get("msg") or overrides.get("msg") == "（不映射）":
                    st.warning("请映射「消息内容」列后再导入。")
                else:
                    st.subheader("4. 导入数据库")
                    if st.button("开始导入", type="primary", key="deal_import_start"):
                        with st.spinner("正在导入（仅写库，不调用 AI）…"):
                            try:
                                result = execute_deal_import(
                                    file_dfs=file_dfs,
                                    column_map=column_map,
                                    run_analyze=False,
                                    qc_cfg=cfg,
                                )
                            except RuntimeError as e:
                                st.error(f"数据库连接失败：{e}")
                                return
                            except Exception as e:
                                st.error(f"导入失败：{core.redact_secrets(str(e), cfg)}")
                                return

                        if not result.get("ok"):
                            st.error(result.get("error") or "导入失败")
                            return

                        summary = result.get("summary") or {}
                        st.session_state.deal_last_import_summary = summary
                        st.session_state.deal_last_import_target = result.get("target") or target
                        st.session_state.deal_learn_skipped = False
                        st.session_state.deal_last_learn_result = None
                        st.success("导入完成，请查看下方结算。")

    if st.session_state.get("deal_last_import_summary"):
        st.divider()
        st.subheader("导入结算")
        _render_deal_import_settlement(
            st.session_state.deal_last_import_summary,
            st.session_state.get("deal_last_import_target") or target,
        )
        if st.session_state.get("deal_learn_skipped"):
            st.caption("你已选择暂不分析。")
            if st.button("现在开始做心理学习", key="deal_learn_unskip"):
                st.session_state.deal_learn_skipped = False
                st.rerun()
        elif not st.session_state.get("deal_last_learn_result"):
            _render_deal_learn_prompt(cfg, default_limit)
        else:
            _render_deal_learning_settlement(st.session_state.deal_last_learn_result)


# ============================================================
# 聊天进度对比标签页
# ============================================================
def _read_compare_reports(uploaded_files):
    """读取上传的质检报告（多工作表），构造按时间排序的报告元信息。"""
    metas = []
    errors = []
    for uf in uploaded_files:
        try:
            sheets = pd.read_excel(io.BytesIO(uf.getvalue()), sheet_name=None, dtype=str)
            metas.append(core.build_report_meta(uf.name, sheets))
        except Exception as e:
            errors.append(f"· {uf.name}：{e}")
    return core.sort_report_metas(metas), errors


def _render_progress_compare_tab(cfg):
    st.caption(
        "上传同一天先后生成的 2~3 份质检报告（`质检报告_*.xlsx`），"
        "按报告时间从早到晚逐段对比同一客户：较早快照指出的问题是否补救、"
        "后续动作是否符合较早快照的建议，未按建议时新做法是否正确。"
        "销售触达次数按 **10 分钟内连续多条合并为 1 次** 统计（较早/新增/今日三列）。"
    )

    st.subheader("1. 上传质检报告")
    uploaded = st.file_uploader(
        "选择 2~3 个质检报告 .xlsx（同一客户群、不同时间生成）",
        type=["xlsx"],
        accept_multiple_files=True,
        key="compare_uploader",
    )

    if not uploaded:
        st.info("请上传由「聊天质检」生成的质检报告。至少 2 份，建议同一天 2~3 份。")
        return

    if len(uploaded) < 2:
        st.warning("进度对比至少需要 2 份报告，请再上传一份。")
        return

    metas, read_errors = _read_compare_reports(uploaded)
    for err in read_errors:
        st.warning(err)
    if len(metas) < 2:
        st.error("可用报告不足 2 份，无法对比。")
        return

    st.session_state.compare_report_metas = metas

    # ---------- 排序结果 ----------
    st.subheader("2. 报告时间排序（早 → 晚）")
    order_rows = []
    for i, m in enumerate(metas):
        ts = m.get("快照时间")
        order_rows.append({
            "顺序": f"R{i + 1}",
            "报告名": m["报告名"],
            "快照时间": ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "未知",
            "时间来源": m.get("快照时间来源", ""),
            "客户数": len(m.get("客户", {})),
        })
    st.dataframe(pd.DataFrame(order_rows), use_container_width=True, hide_index=True)

    pairs = core.build_progress_compare_pairs(metas)
    if not pairs:
        st.warning("相邻报告之间没有可匹配的同一客户（按会话ID/联系人匹配），无法对比。")
        return
    seg_count = len(metas) - 1
    st.caption(f"共 {seg_count} 个对比段、{len(pairs)} 个客户对待分析。")

    # ---------- 开始对比 ----------
    st.subheader("3. 开始进度对比")
    start = st.button("开始进度对比", type="primary", key="compare_start")

    if start:
        progress = st.progress(0, text="准备中…")
        status = st.empty()
        logs = []

        def on_progress(done, total, row):
            progress.progress(done / total if total else 0, text=f"对比进度 {done}/{total}")
            err = row.get("_错误", "")
            if err:
                line = f"[{done}/{total}] {row.get('联系人', '')} → ❌ {core.redact_secrets(err, cfg)}"
            else:
                line = (
                    f"[{done}/{total}] {row.get('联系人', '')}（{row.get('对比段', '')}）→ "
                    f"补救:{row.get('问题是否补救', '')} / 按建议:{row.get('是否按建议执行', '')} / "
                    f"风险{row.get('风险等级', '')}"
                )
            logs.append(line)
            if len(logs) > 8:
                logs.pop(0)
            status.markdown("**最近进度**\n\n" + "\n\n".join(f"- {l}" for l in logs))

        with st.spinner("正在对比，请稍候…"):
            try:
                rows = core.run_progress_compare_batch(pairs, cfg, on_progress=on_progress)
            except Exception as e:
                st.error(f"对比过程异常：{core.redact_secrets(str(e), cfg)}")
                return

        progress.progress(1.0, text="对比完成")
        rows = [r for r in rows if r]
        st.session_state.compare_rows = rows
        st.session_state.compare_df = core.build_progress_compare_dataframe(rows)
        st.session_state.compare_stats = core.compute_progress_compare_stats(rows)

        buf = io.BytesIO()
        core.write_progress_compare_excel(rows, buf)
        st.session_state.compare_report_bytes = buf.getvalue()
        st.session_state.compare_report_name = (
            f"进度对比报告_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        )
        st.success(f"进度对比完成，共 {len(rows)} 个客户对。")

    # ---------- 结果展示 ----------
    stats = st.session_state.get("compare_stats")
    if stats:
        st.subheader("4. 对比结果")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("客户对数", stats.get("对比客户对数", 0))
        c2.metric("高风险", stats.get("高风险数量", 0))
        c3.metric("问题未补救", stats.get("问题未补救数量", 0))
        c4.metric("未按建议执行", stats.get("未按建议执行数量", 0))

    df_compare = st.session_state.get("compare_df")
    if df_compare is not None and len(df_compare) > 0:
        summary_cols = [
            "联系人", "会话ID", "对比段", "较早结果标签", "较晚结果标签",
            "较早销售触达次数", "新增销售触达次数", "今日销售触达次数",
            "阶段变化", "问题是否补救", "是否按建议执行", "风险等级",
        ]
        show_cols = [c for c in summary_cols if c in df_compare.columns]
        st.dataframe(df_compare[show_cols], use_container_width=True, hide_index=True)

        with st.expander("查看每个客户的完整对比详情"):
            for _, r in df_compare.iterrows():
                st.markdown(
                    f"**{r.get('联系人', '')}**（{r.get('对比段', '')} · "
                    f"{r.get('较早报告', '')} → {r.get('较晚报告', '')}）"
                )
                detail = {
                    "阶段变化": r.get("阶段变化", ""),
                    "客服新增动作": r.get("客服新增动作", ""),
                    "问题是否补救": r.get("问题是否补救", ""),
                    "补救说明": r.get("补救说明", ""),
                    "是否按建议执行": r.get("是否按建议执行", ""),
                    "执行偏差评价": r.get("执行偏差评价", ""),
                    "当前最需关注": r.get("当前最需关注", ""),
                    "风险等级": r.get("风险等级", ""),
                }
                for k, v in detail.items():
                    if str(v).strip():
                        st.markdown(f"- **{k}**：{v}")
                if str(r.get("_错误", "")).strip():
                    st.markdown(f"- ❌ **错误**：{r.get('_错误')}")
                delta = str(r.get("新增对话", "")).strip()
                if delta:
                    with st.expander("查看本段新增对话"):
                        st.text(delta[:8000])
                st.divider()

    if st.session_state.get("compare_report_bytes"):
        st.subheader("5. 下载对比报告")
        st.download_button(
            label="下载 Excel 进度对比报告",
            data=st.session_state.compare_report_bytes,
            file_name=st.session_state.compare_report_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            key="compare_download",
        )


if __name__ == "__main__":
    main()
