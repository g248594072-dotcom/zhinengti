# -*- coding: utf-8 -*-
"""删除客户筛选、时间窗口、导入和网页回归测试；不访问外部服务。"""
import sys
import unittest
from datetime import datetime, timedelta, date, time
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
from streamlit.testing.v1 import AppTest

_AGENT = Path(__file__).resolve().parents[1] / "智能体"
sys.path.insert(0, str(_AGENT))
import fetch_delete_salesmartly as fetch
import fetch_delete_daily as daily
import delete_import_core as importer
from fetch_cancel import FetchCancelledError
from fetch_deal_salesmartly import _format_labels

START = datetime(2026, 10, 10)
END = datetime(2026, 10, 11)


def message(when, text="有效客户消息", role=1):
    return {"sender_type": role, "sender": "8", "send_time": int(when.timestamp()), "text": text}


def history(count, in_window=True):
    before = START - timedelta(days=2)
    out = [message(before + timedelta(seconds=i)) for i in range(count - (1 if in_window else 0))]
    if in_window:
        out.append(message(START))
    return out


class TestDeletionFetch(unittest.TestCase):
    def setUp(self):
        self.client = Mock(config=object())
        self.contacts = [
            {"chat_user_id": "seven", "session_id": "s7", "name": "七条客户", "labels": "1"},
            {"chat_user_id": "six", "session_id": "s6", "labels": [{"id": 1}]},
            {"chat_user_id": "outside", "session_id": "outside", "labels": [1]},
            {"chat_user_id": "service", "session_id": "service", "labels": "1"},
            {"chat_user_id": "image", "session_id": "image", "labels": "1"},
            {"chat_user_id": "black", "session_id": "black", "labels": "1,2"},
        ]
        self.calls = []
        def get_pages(endpoint, params=None, **kwargs):
            self.calls.append((endpoint, params))
            if endpoint == "/api/v2/get-label-list":
                return [{"id": 1, "label_name": "客户删除"}, {"id": 2, "label_name": "黑粉"}], 2
            self.assertEqual(params, {"labels": "1"})
            return self.contacts, len(self.contacts)
        self.client.get_all_pages.side_effect = get_pages
        self.messages = {
            "s7": history(7), "s6": history(6), "outside": history(7, False),
            "service": history(7, False) + [message(START, role=2)],
            "image": history(7, False) + [message(START, "图片"), message(START, "1")],
        }

    def run_fetch(self, **kwargs):
        # 缺少会话详情时仍用 contact 元数据及聊天记录，不静默漏掉删除会话。
        with patch.object(fetch, "_fetch_sessions_by_ids", return_value=[]) as sessions, \
             patch.object(fetch, "_fetch_messages_for_sessions", return_value=self.messages) as messages, \
             patch.object(fetch, "fetch_members", return_value=[{"sys_user_id": 8, "nickname": "客服甲"}]):
            df, meta = fetch.fetch_delete_dataframe(self.client, window_start=START, window_end=END, **kwargs)
            return df, meta, messages, sessions

    def test_history_threshold_and_window_communication_with_blacklist(self):
        df, meta, msg_fetch, _ = self.run_fetch()
        self.assertEqual(df["会话ID"].tolist(), ["s7"])
        self.assertEqual(df["客户发言次数"].tolist(), [7])
        self.assertEqual(df["范围内客户发言次数"].tolist(), [1])
        self.assertIn("2026-10-08", df.iloc[0]["会话消息内容"])
        self.assertEqual(meta["excluded_blacklist"], 1)
        self.assertEqual(meta["skipped_insufficient_speech"], 1)
        self.assertEqual(meta["skipped_no_customer_speech"], 3)
        self.assertNotIn("black", msg_fetch.call_args.args[1])
        self.assertEqual(self.calls[0][0], "/api/v2/get-label-list")
        self.assertEqual(self.calls[1][0], "/api/v2/get-contact-list")
        self.assertEqual(meta["session_details_missing"], 5)

    def test_end_exclusive_and_start_inclusive(self):
        self.contacts = [self.contacts[0]]
        self.messages = {"s7": history(7, False) + [message(END)]}
        self.assertTrue(self.run_fetch()[0].empty)
        self.messages["s7"].append(message(START))
        self.assertEqual(len(self.run_fetch()[0]), 1)

    def test_same_session_is_returned_once(self):
        self.contacts = [self.contacts[0], dict(self.contacts[0], chat_user_id="alias")]
        df, _, msg_fetch, _ = self.run_fetch()
        self.assertEqual(len(df), 1)
        self.assertEqual(msg_fetch.call_args.args[1], ["s7"])

    def test_cancelled_before_any_request(self):
        with self.assertRaises(FetchCancelledError):
            self.run_fetch(cancel_check=lambda: True)
        self.client.get_all_pages.assert_not_called()

    def test_bad_window_rejected(self):
        with self.assertRaises(ValueError):
            fetch.fetch_delete_dataframe(self.client, window_start=END, window_end=START)
        self.client.get_all_pages.assert_not_called()

    def test_label_ids_resolved_in_arrays_and_json(self):
        self.assertEqual(_format_labels('[{"id":1},2]', {"1": "删除", "2": "黑粉"}), "删除,黑粉")


class TestDeletionImport(unittest.TestCase):
    def frame(self):
        return pd.DataFrame([
            {"会话ID": str(n), "联系人": str(n), "会话消息内容": "\n".join(['客户 : "你好"'] * n)}
            for n in (6, 7)
        ])

    def test_preview_filters_six_without_writing(self):
        with patch.object(importer, "load_import_config", return_value={}), \
             patch.object(importer, "upsert_sessions") as upsert, patch.object(importer, "init_db") as init:
            result = importer.execute_delete_import(file_dfs=[("test", self.frame())], dry_run=True)
        self.assertTrue(result["ok"])
        self.assertEqual([s["会话ID"] for s in result["sessions"]], ["7"])
        upsert.assert_not_called()
        init.assert_not_called()

    def test_only_seven_is_written_and_marked(self):
        with patch.object(importer, "upsert_sessions", return_value={}) as upsert, \
             patch.object(importer, "mark_sessions_as_deleted", return_value=1) as mark:
            sessions, summary = importer.import_delete_from_file_dfs([("test", self.frame())])
        self.assertEqual([s["会话ID"] for s in upsert.call_args.args[0]], ["7"])
        self.assertEqual([s["会话ID"] for s in mark.call_args.args[0]], ["7"])
        self.assertEqual(summary["settlement"]["skipped_count"], 1)

    def test_daily_runner_forwards_range_and_dry_run_never_imports(self):
        df = self.frame().iloc[1:]
        with patch("deal_import_core.load_import_config", return_value={}), \
             patch("salesmartly_client.load_config", return_value=None), \
             patch("salesmartly_client.SaleSmartlyClient", return_value=Mock()), \
             patch.object(fetch, "fetch_delete_dataframe", return_value=(df, {"delete_contacts": 1})) as get, \
             patch.object(importer, "execute_delete_import") as write:
            out = daily.run_fetch_delete_daily(window_start=START, window_end=END, dry_run=True)
        self.assertTrue(out["ok"])
        self.assertEqual(get.call_args.kwargs["window_start"], START)
        self.assertEqual(get.call_args.kwargs["window_end"], END)
        write.assert_not_called()

    def test_daily_runner_propagates_cancellation(self):
        with patch("deal_import_core.load_import_config", return_value={}), \
             patch("salesmartly_client.load_config", return_value=None), \
             patch("salesmartly_client.SaleSmartlyClient", return_value=Mock()), \
             patch.object(fetch, "fetch_delete_dataframe", side_effect=FetchCancelledError):
            with self.assertRaises(FetchCancelledError):
                daily.run_fetch_delete_daily(window_start=START, window_end=END, dry_run=True)


class TestDeletionWeb(unittest.TestCase):
    def test_custom_window_forwarded_and_invalid_range_disabled(self):
        script = """import streamlit as st
from unittest.mock import patch
import streamlit_app as app
import fetch_delete_daily
app._init_state()
app._render_database_connection = lambda key: None
app._begin_salesmartly_fetch = lambda: (lambda msg: None, st.progress(0), lambda: False)
def run(**kwargs):
    st.session_state['captured_delete_args'] = kwargs
    return {'ok': True, 'meta': {}, 'preview': [], 'window_label': '测试范围'}
with patch('deal_import_core.load_import_config', return_value={}), patch.object(fetch_delete_daily, 'run_fetch_delete_daily', side_effect=run):
    app._render_delete_import_tab(app.core.DEFAULTS)
"""
        at = AppTest.from_string(f"import sys\nsys.path.insert(0, {str(_AGENT)!r})\n" + script, default_timeout=15).run()
        self.assertFalse(at.exception)
        at.date_input(key="delete_window_start_date").set_value(date(2026, 10, 1)).run()
        at.date_input(key="delete_window_end_date").set_value(date(2026, 10, 20)).run()
        at.time_input(key="delete_window_start_time").set_value(time(8, 30)).run()
        at.button(key="delete_api_preview").click().run()
        self.assertFalse(at.exception)
        args = at.session_state["captured_delete_args"]
        self.assertEqual(args["window_start"], datetime(2026, 10, 1, 8, 30))
        self.assertEqual(args["window_end"], datetime(2026, 10, 20))
        self.assertTrue(args["dry_run"])
        at.date_input(key="delete_window_end_date").set_value(date(2026, 9, 30)).run()
        self.assertFalse(at.exception)
        self.assertTrue(at.button(key="delete_api_preview").disabled)
        self.assertTrue(at.button(key="delete_api_import").disabled)


if __name__ == "__main__":
    unittest.main()
