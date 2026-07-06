# -*- coding: utf-8 -*-
"""
大力丸 · 聊天记录质检工具（网页版 Streamlit）
核心逻辑复用 qc_core.py，与桌面版共用。
"""

import io
import importlib
import os
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
        # 聊天进度对比
        "compare_report_metas": None,
        "compare_rows": None,
        "compare_df": None,
        "compare_stats": None,
        "compare_report_bytes": None,
        "compare_report_name": None,
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

    qc_tab, compare_tab = st.tabs(["聊天质检", "聊天进度对比"])
    with qc_tab:
        _render_qc_tab(cfg)
    with compare_tab:
        _render_progress_compare_tab(cfg)


def _render_qc_tab(cfg):
    st.caption("上传 Excel → 确认列映射 → 开始质检 → 下载报告。API Key 由管理员在服务器本地配置，使用者无需也无法填写。")

    _render_time_scope_selector()
    time_scope = st.session_state.get("time_scope", SCOPE_TODAY)

    # ---------- 上传文件 ----------
    st.subheader("1. 上传 Excel")
    uploaded = st.file_uploader(
        "选择一个或多个聊天记录 .xlsx",
        type=["xlsx"],
        accept_multiple_files=True,
    )

    if not uploaded:
        st.info("请上传聊天记录表格。表头通常含：会话ID、联系人、接待成员、社媒渠道、会话消息（或会话消息内容）。")
        return

    try:
        file_dfs, read_errors = _read_uploaded_files(uploaded)
    except ValueError as e:
        st.error(str(e))
        return

    if read_errors:
        for err in read_errors:
            st.warning(err)

    st.session_state.uploaded_dfs = file_dfs

    # 预览第一个文件
    preview_name, preview_df = file_dfs[0]
    st.subheader("2. 数据预览")
    with st.expander(
        f"展开查看数据预览（{preview_name} · 共 {len(preview_df)} 行 · 已上传 {len(file_dfs)} 个文件）",
        expanded=False,
    ):
        st.dataframe(preview_df.head(20), use_container_width=True)

    # ---------- 列映射 ----------
    st.subheader("3. 列名映射（可手动调整）")
    all_columns = list(preview_df.columns)
    if not st.session_state.column_overrides:
        st.session_state.column_overrides, opts, auto_det = _default_override_for_cols(all_columns)
    else:
        opts = core.suggest_column_options(all_columns)
        auto_det = core.detect_columns(all_columns)

    with st.expander("展开调整列名映射（「消息内容」为必填）", expanded=False):
        st.caption("已自动识别列名，如有误请在下方下拉框中修改。")
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

    # ---------- 解析预览 ----------
    column_map = _build_column_map(all_columns, overrides)
    if not column_map.get("msg") or overrides.get("msg") == "（不映射）":
        st.warning("请映射「消息内容」列后再开始质检。")
        return

    if time_scope == SCOPE_CUSTOM:
        cw = _get_custom_window()
        if cw is None or cw[0] >= cw[1]:
            st.warning("请在左侧边栏设置有效的「自定义」起止时间（结束需晚于开始）后再质检。")
            return

    if st.button("预览解析结果", type="secondary"):
        try:
            sessions, diag, window = _load_sessions_scoped(file_dfs, overrides)
            if not sessions:
                hint = "当天窗口内没有可分析的消息" if window else "未能解析出任何会话"
                st.error(f"{hint}，请检查列映射、时间列或分析范围。")
                for d in diag:
                    st.write(d)
            else:
                scope_note = f"（{_scope_label(time_scope)}"
                if window:
                    scope_note += f" · {window['label']}"
                scope_note += "）"
                st.success(f"预计分析 **{len(sessions)}** 通会话{scope_note}")
                tier_counts = {"skip": 0, "light": 0, "full": 0}
                for s in sessions:
                    c, _ = core.count_roles(s["对话"])
                    tier_counts[core.classify_tier(c)] += 1
                st.write(
                    f"分档预估：跳过 {tier_counts['skip']} · "
                    f"轻量 {tier_counts['light']} · 重点 {tier_counts['full']}"
                )
        except Exception as e:
            st.error(f"解析失败：{e}")

    # ---------- 开始质检 ----------
    st.subheader("4. 开始质检")
    enable_sql_db = st.checkbox(
        "将本次导入数据写入 SQL 数据库",
        value=True,
        key="enable_sql_db",
    )
    if enable_sql_db:
        st.caption("以「会话ID」为唯一键入库；重复导入时仅增量追加新增聊天。")
    st.caption(
        "批量 API：按档位分批（跳过→重点→轻量），每档先跑 1 条预热缓存再满并发（默认 24 路），"
        "深度复盘/进度对比同理。"
    )
    enable_deep_review = st.toggle(
        "启用深度复盘（客户发言>10条；常规质检全部完成后再执行）",
        value=False,
        key="enable_deep_review",
    )
    if enable_deep_review:
        st.caption(
            "开启后将在标准质检结束后，对符合条件的客户基于**完整聊天记录**做全面复盘："
            "卡点、不付款原因、顾虑、心理、下一步动作等（不受当天/自定义时间窗口截断）。"
        )
    enable_deal_context = st.toggle(
        "注入成交经验参考（从数据库检索相似已成交案例，辅助质检判断）",
        value=bool(cfg.get("deal_context_enabled", True)),
        key="enable_deal_context",
    )
    if enable_deal_context:
        st.caption(
            "按客户阶段/顾虑/话术匹配 3～5 条历史成交复盘，写入 prompt「参考案例」区；"
            "报告会显示「成交参考案例数/摘要」。"
        )
    run_ab_validation = st.checkbox(
        "效果验证：首通会话对比「有/无成交参考」两次质检（多耗 1 次 API）",
        value=False,
        key="run_ab_validation",
    )
    start = st.button("开始质检", type="primary", use_container_width=False)

    if start:
        try:
            sessions, diag, window = _load_sessions_scoped(file_dfs, overrides)
        except Exception as e:
            st.error(f"Excel 解析失败：{e}")
            return

        if not sessions:
            if window:
                st.error(
                    f"当天窗口（{window['label']}）内没有符合发言时间的消息。\n\n"
                    "请确认已映射「发言时间」列，或消息中含可解析的 `[时间]`，或切换为「全部」。"
                )
            else:
                st.error("未解析出任何会话，请检查文件与列映射。")
            return

        if window:
            st.caption(f"已按发言时间过滤（{window['label']}），共 {len(sessions)} 通会话")

        # ---------- SQL 入库（使用完整聊天记录，不受时间窗口截断）----------
        if st.session_state.get("enable_sql_db", True):
            try:
                from db import init_db, upsert_sessions

                init_db()
                full_result = core.load_sessions(
                    file_dfs=file_dfs,
                    column_map=overrides,
                    time_scope=SCOPE_ALL,
                )
                full_sessions = full_result[0] if full_result else []
                source_name = file_dfs[0][0] if file_dfs else None
                db_result = upsert_sessions(full_sessions, source_file=source_name)
                st.success(
                    f"SQL 入库完成："
                    f"新增客户 {db_result['customers_created']} 个，"
                    f"更新客户 {db_result['customers_updated']} 个，"
                    f"新增会话 {db_result['sessions_created']} 个，"
                    f"更新会话 {db_result['sessions_updated']} 个，"
                    f"新增消息 {db_result['messages_inserted']} 条，"
                    f"无新增聊天 {db_result['unchanged_sessions']} 个，"
                    f"冲突 {db_result['conflicts']} 个"
                )
                if db_result.get("skipped_no_session_id"):
                    st.warning(
                        f"有 {db_result['skipped_no_session_id']} 条会话缺少会话ID，已跳过入库。"
                    )
                if db_result["conflicts"] > 0:
                    st.warning(
                        "部分会话导入冲突：数据库最后一句话在新导入聊天中找不到。"
                        "为避免重复污染，系统未插入这些会话的新聊天，请人工检查。"
                    )
                    st.write(db_result["conflict_session_ids"])
                if db_result.get("errors"):
                    with st.expander("入库警告详情"):
                        for err in db_result["errors"][:20]:
                            st.text(err)
            except RuntimeError as e:
                st.error(f"SQL 数据库连接失败：{e}")
            except Exception as e:
                st.error(f"SQL 入库失败：{e}")

        progress = st.progress(0, text="准备中…")
        status = st.empty()
        log_area = st.empty()
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
                        cfg,
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
        scope_tag = _scope_label(st.session_state.time_scope)
        st.session_state.report_name = f"质检报告_{scope_tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        st.session_state.report_scope = scope_tag

        done_msg = f"质检完成（{_scope_label(time_scope)}）"
        if deep_rows:
            done_msg += f"，深度复盘 **{len(deep_rows)}** 通"
        st.success(done_msg)

        # ---------- 质检结果入库 ----------
        try:
            from db import save_qc_results, mark_deal_customers_from_qc

            save_result = save_qc_results(results)
            deal_result = mark_deal_customers_from_qc(results)
            st.info(
                f"质检结果已入库：{save_result['saved']} 条；"
                f"新增/更新成交客户：{deal_result['deal_customers']} 个"
            )
            if save_result.get("skipped"):
                st.warning(
                    f"有 {save_result['skipped']} 条质检结果缺少会话ID，无法入库。"
                )
        except RuntimeError as e:
            st.warning(f"质检结果入库跳过（数据库不可用）：{e}")
        except Exception as e:
            st.warning(f"质检结果入库失败：{e}")

    # ---------- 成交客户心理学习 ----------
    st.subheader("成交客户学习")
    st.caption("对已成交且尚未分析的客户，调用 AI 做成交心理复盘（不会重复分析已有记录）。")
    if st.button("分析新增成交客户", type="secondary", key="analyze_deals_btn"):
        try:
            from deal_intelligence import analyze_unlearned_deals

            with st.spinner("正在分析成交客户…"):
                result = analyze_unlearned_deals(cfg, limit=20)
            st.success(
                f"成交学习完成："
                f"待分析 {result['total']} 个，"
                f"成功 {result['success']} 个，"
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

    # ---------- 统计与下载 ----------
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
            label="下载 Excel 质检报告",
            data=st.session_state.report_bytes,
            file_name=st.session_state.report_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )


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
