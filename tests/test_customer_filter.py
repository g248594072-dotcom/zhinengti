# -*- coding: utf-8 -*-
"""客户筛选与分组选择回归测试；使用模拟数据，不访问外部服务。"""
import io
import sys
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

_AGENT = Path(__file__).resolve().parents[1] / "智能体"
sys.path.insert(0, str(_AGENT))
import qc_core as core


def session(count, name="测试客户"):
    return {
        "会话ID": name,
        "联系人": name,
        "接待成员": "测试客服",
        "渠道": "微信",
        "对话": "\n".join(
            f'[2026-10-16 10:00:{i:02d}] 客户 : "消息{i}"'
            for i in range(count)
        ),
    }


class TestCustomerFilter(unittest.TestCase):
    def test_strict_threshold_and_exact_columns(self):
        df = core.build_customer_filter_dataframe([session(3, "三句"), session(4, "四句")])
        self.assertEqual(list(df.columns), ["客户发言时间", "客户发言次数", "客户名字", "接待客服名字"])
        self.assertEqual(df.to_dict("records"), [{
            "客户发言时间": "2026-10-16 10:00:03", "客户发言次数": 4,
            "客户名字": "四句", "接待客服名字": "测试客服",
        }])

    def test_ignores_invalid_customer_and_service_messages(self):
        s = session(3)
        s["对话"] += '\n客户 : "图片"\n客户 : "1"\n客服 : "您好"\n自动化 : "欢迎"'
        self.assertTrue(core.build_customer_filter_dataframe([s]).empty)
        s["对话"] += '\n[2026-10-16 11:00:00] 客户 : "想了解价格"'
        s["对话"] += '\n[2026-10-16 12:00:00] 客户 : "图片"'
        # 源记录顺序可能不按时间排列，取有效发言的最大时间。
        s["对话"] += '\n[2026-10-16 09:00:00] 客户 : "补充信息"'
        row = core.build_customer_filter_dataframe([s]).iloc[0]
        self.assertEqual(row["客户发言次数"], 5)
        self.assertEqual(row["客户发言时间"], "2026-10-16 11:00:00")

    def test_uses_scoped_dialog_instead_of_full_history(self):
        s = session(4)
        s["对话_全量"] = s["对话"]
        scoped, _ = core.filter_sessions_by_window(
            [s], datetime(2026, 10, 16, 10, 0, 1), datetime(2026, 10, 16, 11),
        )
        self.assertEqual(len(scoped), 1)
        self.assertTrue(core.build_customer_filter_dataframe(scoped).empty)

    def test_missing_timestamp_is_blank(self):
        s = session(4)
        s["对话"] = '\n'.join(['客户 : "您好"'] * 4)
        self.assertEqual(core.build_customer_filter_dataframe([s]).iloc[0]["客户发言时间"], "")

    def test_empty_result_keeps_four_headers(self):
        for sessions in ([], [session(3)]):
            df = core.build_customer_filter_dataframe(sessions)
            self.assertTrue(df.empty)
            self.assertEqual(list(df.columns), core.CUSTOMER_FILTER_COLUMNS)


class TestStreamlitCustomerFilter(unittest.TestCase):
    def app(self, body):
        return AppTest.from_string(
            f"import sys\nsys.path.insert(0, {str(_AGENT)!r})\n" + body,
            default_timeout=15,
        )

    def test_group_select_preserves_other_groups_and_allows_uncheck(self):
        at = self.app('''import streamlit as st
import streamlit_app as app
groups = {"一转高": [(1, "客服甲"), (2, "客服乙")], "一转西": [(3, "客服丙")]}
app._render_qc_agent_selector(groups, {})
''').run()
        self.assertFalse(at.exception)
        at.checkbox(key="qc_agent_3").check().run()
        at.button(key="qc_agents_select_group_一转高").click().run()
        self.assertFalse(at.exception)
        self.assertTrue(all(at.checkbox(key=f"qc_agent_{i}").value for i in (1, 2, 3)))
        at.checkbox(key="qc_agent_1").uncheck().run()
        self.assertFalse(at.checkbox(key="qc_agent_1").value)
        self.assertTrue(at.checkbox(key="qc_agent_2").value)
        at.button(key="qc_agents_select_group_一转高").click().run()
        self.assertTrue(at.checkbox(key="qc_agent_1").value)
        at.button(key="qc_agents_clear").click().run()
        self.assertFalse(any(at.checkbox(key=f"qc_agent_{i}").value for i in (1, 2, 3)))

    def test_filter_start_download_and_mode_switch_without_ai_or_database(self):
        sessions = [session(3, "三句"), session(4, "四句")]
        script = '''import streamlit as st
from types import SimpleNamespace
from unittest.mock import patch
import streamlit_app as app
import salesmartly_client
app._init_state()
app._render_time_scope_selector = lambda: st.session_state.setdefault("time_scope", app.SCOPE_ALL)
app._cached_salesmartly_members = lambda: ([], {"一转高": [(1, "测试客服")]}, {1: "测试客服"})
app._begin_salesmartly_fetch = lambda: (lambda msg: None, st.progress(0), lambda: False)
sessions = SESSIONS_PLACEHOLDER
fetch = SimpleNamespace(
    fetch_qc_dataframe=lambda *args, **kwargs: (None, {}),
    dataframe_to_sessions=lambda *args, **kwargs: (sessions, None),
)
app._reload_fetch_qc_salesmartly = lambda: fetch
fail = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应调用 AI 或写入质检数据库"))
db = SimpleNamespace(save_qc_results=fail, mark_deal_customers_from_qc=fail)
with patch.object(salesmartly_client, "load_config", return_value=None), \
     patch.object(salesmartly_client, "SaleSmartlyClient", return_value=None), \
     patch.object(app.core, "run_qc_batch", side_effect=fail), \
     patch.object(app.core, "run_deep_review_batch", side_effect=fail), \
     patch.object(app.core, "compare_qc_deal_context", side_effect=fail), \
     patch.dict("sys.modules", {"db": db}):
    app._render_qc_tab(app.core.DEFAULTS)
'''.replace("SESSIONS_PLACEHOLDER", repr(sessions))
        at = self.app(script).run()
        self.assertFalse(at.exception)
        at.button(key="qc_agents_select_group_一转高").click().run()
        at.toggle(key="enable_deep_review").set_value(True).run()
        at.checkbox(key="run_ab_validation").check().run()
        at.toggle(key="qc_filter_only").set_value(True).run()
        self.assertEqual(at.button(key="qc_start_btn").label, "开始筛选")
        self.assertNotIn("enable_deep_review", [w.key for w in at.toggle])
        at.button(key="qc_start_btn").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["report_kind"], "filter")
        self.assertIsNone(at.session_state["last_stats"])
        report = pd.read_excel(io.BytesIO(at.session_state["report_bytes"]), sheet_name=None)
        self.assertEqual(list(report), ["客户筛选"])
        self.assertEqual(list(report["客户筛选"].columns), core.CUSTOMER_FILTER_COLUMNS)
        self.assertEqual(report["客户筛选"]["客户名字"].tolist(), ["四句"])
        self.assertEqual(report["客户筛选"]["客户发言次数"].tolist(), [4])
        self.assertEqual(at.get("download_button")[0].label, "下载 Excel 客户筛选表")
        at.toggle(key="qc_filter_only").set_value(False).run()
        self.assertFalse(at.exception)
        self.assertEqual(at.button(key="qc_start_btn").label, "开始质检")
        self.assertIn("enable_deep_review", [w.key for w in at.toggle])
        self.assertEqual(at.session_state["report_kind"], "filter")


if __name__ == "__main__":
    unittest.main()
