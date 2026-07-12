# -*- coding: utf-8 -*-
"""qc_core 最小单元测试（不调用 API）。"""

import os
import sys
import unittest

# 将 智能体 目录加入路径
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT = os.path.join(_ROOT, "智能体")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

import qc_core as core  # noqa: E402


class TestCountRoles(unittest.TestCase):
    def test_with_timestamp_halfwidth_colon(self):
        text = (
            '[2026-01-01 10:00:00] 客户 : "hi"\n'
            '[2026-01-01 10:01:00] 乐乐 : "hello"\n'
            '[2026-01-01 10:02:00] 自动化 : "auto"\n'
        )
        cust, serv = core.count_roles(text)
        self.assertEqual(cust, 1)
        self.assertEqual(serv, 1)

    def test_with_timestamp_fullwidth_colon(self):
        text = (
            '[2026-01-01 10:00:00] 客户： "hi"\n'
            '[2026-01-01 10:01:00] 刘星： "reply"\n'
        )
        cust, serv = core.count_roles(text)
        self.assertEqual(cust, 1)
        self.assertEqual(serv, 1)

    def test_without_timestamp(self):
        text = (
            '客户 : "a"\n'
            '客户： "b"\n'
            '乐乐 : "c"\n'
            '刘娜娜： "d"\n'
        )
        cust, serv = core.count_roles(text)
        self.assertEqual(cust, 2)
        self.assertEqual(serv, 2)

    def test_power_tag_counts_as_customer_speech(self):
        text = (
            '[2026-07-10 10:00:00] 客户 : "【💪50+】"\n'
            '[2026-07-10 10:01:00] 乐乐 : "hello"\n'
        )
        cust, serv = core.count_roles(text)
        self.assertEqual(cust, 1)
        self.assertEqual(serv, 1)

    def test_system_roles_not_counted_as_service(self):
        text = (
            '客户 : "x"\n'
            '自动化 : "y"\n'
            '自定义： "z"\n'
            '其他平台 : "w"\n'
            '乐乐 : "ok"\n'
        )
        cust, serv = core.count_roles(text)
        self.assertEqual(cust, 1)
        self.assertEqual(serv, 1)

    def test_normalize_contact_name_as_customer_speaker(self):
        dialog = (
            '[2026-07-12 10:00:00] Jay Bullock : "hi"\n'
            '[2026-07-12 10:01:00] 马俊鹏 : "hello"\n'
            '[2026-07-12 10:02:00] Jay Bullock : "price?"\n'
        )
        normalized = core.normalize_customer_speaker_in_dialog(dialog, "Jay Bullock")
        cust, serv = core.count_roles(normalized)
        self.assertEqual(cust, 2)
        self.assertEqual(serv, 1)
        self.assertIn("客户 :", normalized)
        self.assertNotIn("Jay Bullock :", normalized)


class TestClassifyTier(unittest.TestCase):
    def test_skip_light_full_boundaries(self):
        self.assertEqual(core.classify_tier(0), "skip")
        self.assertEqual(core.classify_tier(2), "skip")
        self.assertEqual(core.classify_tier(3), "light")
        self.assertEqual(core.classify_tier(4), "light")
        self.assertEqual(core.classify_tier(6), "light")
        self.assertEqual(core.classify_tier(7), "full")
        self.assertEqual(core.classify_tier(20), "full")

    def test_tier_follows_customer_count_in_dialog(self):
        lines = []
        for i in range(5):
            lines.append(f'客户 : "msg{i}"')
        text = "\n".join(lines)
        cust, _ = core.count_roles(text)
        self.assertEqual(cust, 5)
        self.assertEqual(core.classify_tier(cust), "light")


class TestQcTiming(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(core.format_duration(45), "45秒")
        self.assertEqual(core.format_duration(125), "2分5秒")
        self.assertEqual(core.format_duration(None), "—")

    def test_summarize_qc_timing(self):
        from datetime import datetime

        timing = {
            "started_at": datetime(2026, 6, 15, 10, 0, 0),
            "ended_at": datetime(2026, 6, 15, 10, 2, 30),
            "wall_seconds": 150.0,
            "concurrency": 8,
            "light": {"count": 2, "total_seconds": 40.0},
            "full": {"count": 1, "total_seconds": 60.0},
            "skip": {"count": 1, "total_seconds": 0.5},
        }
        s = core.summarize_qc_timing(timing)
        self.assertEqual(s["开始时间"], "2026-06-15 10:00:00")
        self.assertEqual(s["结束时间"], "2026-06-15 10:02:30")
        self.assertEqual(s["总耗时文本"], "2分30秒")
        self.assertEqual(s["轻量数量"], 2)
        self.assertEqual(s["完整数量"], 1)
        self.assertEqual(s["轻量单通耗时秒"], 20.0)
        self.assertEqual(s["完整单通耗时秒"], 60.0)
        self.assertEqual(s["分析客户数"], 3)
        self.assertEqual(s["分析客户均价秒"], 50.0)


class TestLowIntentReplyWindow(unittest.TestCase):
    def _row(self, cust_h, cust_m, serv_h, serv_m, gap_extra_min=0):
        from datetime import datetime

        cust = datetime(2026, 6, 15, cust_h, cust_m, 0)
        serv = datetime(2026, 6, 15, serv_h, serv_m, 0)
        if gap_extra_min:
            from datetime import timedelta

            serv = cust + timedelta(minutes=gap_extra_min)
        return {
            "客户第一次发言": cust.strftime("%Y-%m-%d %H:%M:%S"),
            "客服第一次发言": serv.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def test_window_boundaries(self):
        from datetime import datetime

        self.assertFalse(core.is_low_intent_reply_window(datetime(2026, 6, 15, 9, 29)))
        self.assertTrue(core.is_low_intent_reply_window(datetime(2026, 6, 15, 9, 30)))
        self.assertTrue(core.is_low_intent_reply_window(datetime(2026, 6, 15, 11, 59)))
        self.assertFalse(core.is_low_intent_reply_window(datetime(2026, 6, 15, 12, 0)))
        self.assertFalse(core.is_low_intent_reply_window(datetime(2026, 6, 15, 13, 0)))
        self.assertTrue(core.is_low_intent_reply_window(datetime(2026, 6, 15, 13, 30)))
        self.assertTrue(core.is_low_intent_reply_window(datetime(2026, 6, 15, 18, 30)))
        self.assertFalse(core.is_low_intent_reply_window(datetime(2026, 6, 15, 20, 0)))

    def test_no_red_outside_window_even_if_slow(self):
        # 08:00 客户发言，客服 30 分钟后回复 → 不标红
        row = self._row(8, 0, 8, 30)
        self.assertEqual(core.problem_columns_for_low_intent_row(row), [])
        # 12:30 午休 → 不标红
        row = self._row(12, 30, 13, 0)
        self.assertEqual(core.problem_columns_for_low_intent_row(row), [])

    def test_red_inside_window_when_slow(self):
        row = self._row(10, 0, 0, 0, gap_extra_min=15)
        self.assertEqual(core.problem_columns_for_low_intent_row(row), ["客服第一次发言"])

    def test_no_red_inside_window_when_fast(self):
        row = self._row(10, 0, 0, 0, gap_extra_min=5)
        self.assertEqual(core.problem_columns_for_low_intent_row(row), [])


class TestDeepReviewEligibility(unittest.TestCase):
    def test_qualifies_threshold(self):
        short = {"对话": '客户 : "a"\n' * 10}
        long = {"对话": '客户 : "a"\n' * 11}
        self.assertFalse(core.qualifies_for_deep_review(short))
        self.assertTrue(core.qualifies_for_deep_review(long))
        scoped = {"对话": '客户 : "a"\n' * 5, "对话_全量": '客户 : "a"\n' * 12}
        self.assertTrue(core.qualifies_for_deep_review(scoped))


class TestProgressCompare(unittest.TestCase):
    def test_parse_report_timestamp_from_name(self):
        from datetime import datetime

        ts = core.parse_report_timestamp_from_name("质检报告_自定义_20260615_105523.xlsx")
        self.assertEqual(ts, datetime(2026, 6, 15, 10, 55, 23))
        self.assertIsNone(core.parse_report_timestamp_from_name("某报告.xlsx"))

    def test_sort_report_metas_by_name_time(self):
        metas = [
            {"报告名": "质检报告_b_20260615_140000.xlsx", "客户": {},
             "快照时间": core.parse_report_timestamp_from_name("x_20260615_140000.x")},
            {"报告名": "质检报告_a_20260615_090000.xlsx", "客户": {},
             "快照时间": core.parse_report_timestamp_from_name("x_20260615_090000.x")},
        ]
        ordered = core.sort_report_metas(metas)
        self.assertEqual(ordered[0]["报告名"], "质检报告_a_20260615_090000.xlsx")
        self.assertEqual(ordered[1]["报告名"], "质检报告_b_20260615_140000.xlsx")

    def test_sort_report_metas_no_time_goes_last(self):
        metas = [
            {"报告名": "无时间.xlsx", "客户": {}, "快照时间": None},
            {"报告名": "有时间.xlsx", "客户": {},
             "快照时间": core.parse_report_timestamp_from_name("x_20260615_090000.x")},
        ]
        ordered = core.sort_report_metas(metas)
        self.assertEqual(ordered[0]["报告名"], "有时间.xlsx")
        self.assertEqual(ordered[1]["报告名"], "无时间.xlsx")

    def test_extract_dialogue_delta_with_timestamps(self):
        earlier = (
            '[2026-06-15 10:00:00] 客户 : "hi"\n'
            '[2026-06-15 10:01:00] 乐乐 : "hello"\n'
        )
        later = (
            '[2026-06-15 10:00:00] 客户 : "hi"\n'
            '[2026-06-15 10:01:00] 乐乐 : "hello"\n'
            '[2026-06-15 11:00:00] 客户 : "ok price?"\n'
            '[2026-06-15 11:05:00] 乐乐 : "400 AUD"\n'
        )
        delta = core.extract_dialogue_delta(earlier, later)
        self.assertIn("ok price?", delta)
        self.assertIn("400 AUD", delta)
        self.assertNotIn("hello", delta)

    def test_extract_dialogue_delta_no_timestamps_suffix(self):
        earlier = '客户 : "a"\n乐乐 : "b"\n'
        later = '客户 : "a"\n乐乐 : "b"\n客户 : "c"\n'
        delta = core.extract_dialogue_delta(earlier, later)
        self.assertEqual(delta.strip(), '客户 : "c"')

    def test_build_progress_compare_pairs(self):
        r1 = {
            "报告名": "R1_20260615_090000.xlsx",
            "快照时间": core.parse_report_timestamp_from_name("x_20260615_090000.x"),
            "客户": {
                ("id", "S1"): {"会话ID": "S1", "联系人": "Tom",
                               "原始对话": '[2026-06-15 09:00:00] 客户 : "hi"\n'},
            },
        }
        r2 = {
            "报告名": "R2_20260615_140000.xlsx",
            "快照时间": core.parse_report_timestamp_from_name("x_20260615_140000.x"),
            "客户": {
                ("id", "S1"): {"会话ID": "S1", "联系人": "Tom",
                               "原始对话": '[2026-06-15 09:00:00] 客户 : "hi"\n'
                                         '[2026-06-15 13:00:00] 客户 : "price?"\n'},
                ("id", "S2"): {"会话ID": "S2", "联系人": "Jim", "原始对话": ""},
            },
        }
        pairs = core.build_progress_compare_pairs([r1, r2])
        self.assertEqual(len(pairs), 1)
        p = pairs[0]
        self.assertEqual(p["会话ID"], "S1")
        self.assertEqual(p["对比段"], "1→2")
        self.assertIn("price?", p["新增对话"])

    def test_load_report_customers_merges_sheets(self):
        import pandas as pd

        quality = pd.DataFrame([{"会话ID": "S1", "联系人": "Tom", "主要问题": "未报价"}])
        low = pd.DataFrame([{"会话ID": "S2", "联系人": "Jim", "主要问题": ""}])
        deep = pd.DataFrame([{"会话ID": "S1", "推荐下一步动作": "约次日跟进"}])
        sheets = {
            core.REPORT_SHEET_QUALITY: quality,
            core.REPORT_SHEET_LOW_INTENT: low,
            core.REPORT_SHEET_DEEP_REVIEW: deep,
        }
        customers = core.load_report_customers(sheets)
        self.assertIn(("id", "S1"), customers)
        self.assertIn(("id", "S2"), customers)
        self.assertEqual(customers[("id", "S1")]["推荐下一步动作"], "约次日跟进")

    def test_compute_progress_compare_stats(self):
        rows = [
            {"对比段": "1→2", "问题是否补救": "是", "是否按建议执行": "是", "风险等级": "低"},
            {"对比段": "1→2", "问题是否补救": "否", "是否按建议执行": "否", "风险等级": "高"},
        ]
        stats = core.compute_progress_compare_stats(rows)
        self.assertEqual(stats["对比客户对数"], 2)
        self.assertEqual(stats["高风险数量"], 1)
        self.assertEqual(stats["问题未补救数量"], 1)
        self.assertEqual(stats["未按建议执行数量"], 1)

    def test_count_service_touch_bursts_10min(self):
        gap = core.PROGRESS_COMPARE_TOUCH_GAP_MINUTES
        base = "[2026-06-15 10:00:00] 乐乐 :"
        # 10:00, 10:05, 10:09 → 同一触达
        t1 = (
            f'{base} "a"\n'
            f'[2026-06-15 10:05:00] 乐乐 : "b"\n'
            f'[2026-06-15 10:09:00] 乐乐 : "c"\n'
        )
        self.assertEqual(core.count_service_touch_bursts(t1, gap_minutes=gap), 1)
        # 10:00, 10:05, 10:09 → 同一触达；10:20 距上次 >10 分钟 → 第二次触达
        t2 = t1 + '[2026-06-15 10:20:00] 乐乐 : "d"\n'
        self.assertEqual(core.count_service_touch_bursts(t2, gap_minutes=gap), 2)

    def test_compare_progress_touch_counts(self):
        earlier_dialog = (
            '[2026-06-15 09:00:00] 客户 : "hi"\n'
            '[2026-06-15 09:01:00] 乐乐 : "hello"\n'
            '[2026-06-15 09:02:00] 乐乐 : "more"\n'
        )
        later_dialog = (
            earlier_dialog
            + '[2026-06-15 13:00:00] 客户 : "price?"\n'
            + '[2026-06-15 13:01:00] 乐乐 : "A plan"\n'
            + '[2026-06-15 13:20:00] 乐乐 : "follow up"\n'
        )
        delta = core.extract_dialogue_delta(earlier_dialog, later_dialog)
        pair = {
            "联系人": "Tom", "会话ID": "S1", "接待成员": "", "渠道": "",
            "对比段": "1→2", "较早报告": "R1", "较晚报告": "R2",
            "较早": {"原始对话": earlier_dialog, "结果标签": "优2"},
            "较晚": {"原始对话": later_dialog, "结果标签": "优2"},
            "新增对话": delta,
        }
        row = core.compute_progress_touch_counts(pair)
        self.assertEqual(row["较早销售触达次数"], 1)
        self.assertEqual(row["新增销售触达次数"], 2)
        self.assertEqual(row["今日销售触达次数"], 3)


class TestQcTierBatching(unittest.TestCase):
    def _session(self, n_customer_lines):
        lines = [f'[2026-06-15 10:0{i}:00] 客户 : "m{i}"\n' for i in range(n_customer_lines)]
        return {"对话": "".join(lines), "会话ID": f"S{n_customer_lines}"}

    def test_group_session_indices_by_tier_order(self):
        sessions = [
            self._session(2),   # skip
            self._session(10),  # full
            self._session(5),   # light
            self._session(8),   # full
        ]
        waves = core._group_session_indices_by_tier(sessions)
        self.assertEqual([t for t, _ in waves], ["skip", "full", "light"])
        by_tier = {t: items for t, items in waves}
        self.assertEqual([idx for idx, _ in by_tier["skip"]], [0])
        self.assertEqual([idx for idx, _ in by_tier["full"]], [1, 3])
        self.assertEqual([idx for idx, _ in by_tier["light"]], [2])

    def test_group_deep_review_by_note(self):
        eligible = [
            {"对话_全量": ""},
            {"对话_全量": "full history"},
            {"对话": "x"},
        ]
        waves = core._group_deep_review_by_note(eligible)
        self.assertEqual(len(waves), 2)
        self.assertEqual([i for i, _ in waves[0]], [0, 2])
        self.assertEqual([i for i, _ in waves[1]], [1])


    def test_parallel_with_cache_warmup_serial_first(self):
        order = []

        def work(n):
            order.append(n)
            return n

        out = core._run_parallel_with_cache_warmup([1, 2, 3], 8, work, cache_warmup=True)
        self.assertEqual(sorted(out), [1, 2, 3])
        self.assertEqual(order[0], 1)

    def test_parallel_with_cache_warmup_single_item(self):
        self.assertEqual(
            core._run_parallel_with_cache_warmup([42], 24, lambda x: x * 2, cache_warmup=True),
            [84],
        )


class TestDealContext(unittest.TestCase):
    def test_infer_dialog_signals_price_concern(self):
        from deal_context import infer_dialog_signals

        dialog = (
            '客户 : "How much does it cost? AUD 400 is expensive"\n'
            '乐乐 : "Let me explain the plan"\n'
        )
        sig = infer_dialog_signals(dialog)
        self.assertIn("价格", sig["concerns"])

    def test_score_deal_relevance_objection_match(self):
        from deal_context import infer_dialog_signals, score_deal_relevance

        dialog = '客户 : "Is this a scam? I need proof"\n'
        signals = infer_dialog_signals(dialog)
        deal = {
            "main_objection": "客户主要顾虑是信任，担心被骗",
            "customer_profile": "澳洲中老年男性",
            "deal_stage_summary": "进线后逐步建立信任",
            "customer_psychology_path": "",
            "recommended_agent_rules": "先给资质再讲方案",
            "reusable_sales_experience": "",
            "analyzed_at": None,
        }
        score = score_deal_relevance(dialog, signals, deal)
        self.assertGreater(score, 2.0)

    def test_build_deal_reference_note(self):
        from deal_context import build_deal_reference_note

        note = build_deal_reference_note([{
            "contact_name": "Tom",
            "_match_reason": "顾虑≈价格",
            "recommended_agent_rules": "报价前先确认症状",
            "reusable_sales_experience": "分条报价",
        }])
        self.assertIn("参考成交案例", note)
        self.assertIn("Tom", note)
        self.assertIn("智能体判断规则", note)

    def test_diff_qc_rows(self):
        from deal_context import diff_qc_rows

        diffs = diff_qc_rows(
            {"是否合格": "合格", "主要问题": "无"},
            {"是否合格": "不合格", "主要问题": "报价不完整"},
        )
        self.assertEqual(len(diffs), 2)
        self.assertEqual(diffs[0]["字段"], "是否合格")

    def test_resolve_deal_context_disabled_returns_four_values(self):
        out = core._resolve_deal_context({"deal_context_enabled": False}, "客户 : hi", None)
        self.assertEqual(out, ("", [], "", ""))

    def test_qc_skip_tier_no_deal_context(self):
        """规则筛选档不走成交经验检索，也不调 API。"""
        session = {
            "会话ID": "s1",
            "联系人": "Test",
            "接待成员": "客服A",
            "渠道": "微信",
            "对话": '客户 : "hi"\n乐乐 : "hello"\n',
        }
        row, tier = core.qc_one_session({"deal_context_enabled": True}, session)
        self.assertEqual(tier, "skip")
        self.assertEqual(row.get("结果标签"), "规则筛选")
        self.assertEqual(row.get("成交参考案例数"), "")
        self.assertNotIn("_错误", row)


class TestConcurrencyHelpers(unittest.TestCase):
    def test_max_concurrency_pro(self):
        self.assertEqual(core.max_concurrency_for_model("deepseek-v4-pro"), 500)

    def test_max_concurrency_flash(self):
        self.assertEqual(core.max_concurrency_for_model("deepseek-v4-flash"), 2500)

    def test_default_concurrency(self):
        self.assertEqual(core.default_concurrency_for_model("deepseek-v4-pro"), 100)


class TestEnrichSystemPrompt(unittest.TestCase):
    def test_enrich_skips_empty_supplement(self):
        base = "# 基础 prompt"
        out = core.enrich_system_prompt(base, "full")
        self.assertEqual(out, base)


if __name__ == "__main__":
    unittest.main()
