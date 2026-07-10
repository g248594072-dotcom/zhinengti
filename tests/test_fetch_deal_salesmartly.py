# -*- coding: utf-8 -*-
"""fetch_deal_salesmartly 单元测试（不调用真实 API）。"""

import os
import sys
import unittest
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT = os.path.join(_ROOT, "智能体")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

from fetch_deal_salesmartly import (  # noqa: E402
    build_deal_pattern,
    build_member_groups,
    date_tag_name,
    is_deal_contact,
    yesterday_for_run,
)


class TestDealFetchHelpers(unittest.TestCase):
    def test_date_tag_name(self):
        self.assertEqual(date_tag_name(datetime(2026, 7, 9)), "7..9")
        self.assertEqual(date_tag_name(datetime(2026, 12, 1)), "12..1")

    def test_yesterday_for_run(self):
        day = yesterday_for_run(datetime(2026, 7, 10, 15, 30))
        self.assertEqual(day, datetime(2026, 7, 9))

    def test_is_deal_contact(self):
        pattern = build_deal_pattern(["全款", "定金", "分期"])
        self.assertTrue(is_deal_contact({"labels": "7..9,全款客户"}, pattern))
        self.assertTrue(is_deal_contact({"labels": "定金已付"}, pattern))
        self.assertTrue(is_deal_contact({"labels": "分期方案"}, pattern))
        self.assertFalse(is_deal_contact({"labels": "7..9,一般客户"}, pattern))
        self.assertFalse(is_deal_contact({"labels": ""}, pattern))

    def test_build_deal_pattern_empty_fallback(self):
        pattern = build_deal_pattern([])
        self.assertTrue(pattern.search("全款"))

    def test_build_member_groups_dedupes_multi_group(self):
        members = [
            {
                "sys_user_id": 907873,
                "nickname": "张三",
                "groups": [
                    {"group_name": "一转高"},
                    {"group_name": "二转"},
                ],
            }
        ]
        groups = build_member_groups(members)
        all_uids = [uid for pairs in groups.values() for uid, _ in pairs]
        self.assertEqual(all_uids, [907873])
        self.assertIn(907873, dict(groups["一转高"]))


if __name__ == "__main__":
    unittest.main()
