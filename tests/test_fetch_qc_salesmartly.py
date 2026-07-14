# -*- coding: utf-8 -*-
"""fetch_qc_salesmartly 单元测试（不调用真实 API）。"""

import os
import sys
import unittest
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT = os.path.join(_ROOT, "智能体")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

from fetch_qc_salesmartly import (  # noqa: E402
    _filter_contacts_by_user_last_reply,
    _parse_api_timestamp,
    customer_replied_in_window,
)


class TestUserLastReplyFilter(unittest.TestCase):
    def setUp(self):
        self.window = (
            datetime(2026, 7, 13, 20, 0, 0),
            datetime(2026, 7, 14, 20, 0, 0),
        )

    def test_parse_millisecond_timestamp(self):
        dt = datetime(2026, 7, 14, 10, 0, 0)
        ms = int(dt.timestamp() * 1000)
        self.assertEqual(_parse_api_timestamp(ms), dt)

    def test_parse_second_timestamp(self):
        dt = datetime(2026, 7, 14, 10, 0, 0)
        self.assertEqual(_parse_api_timestamp(int(dt.timestamp())), dt)

    def test_zero_or_missing_is_none(self):
        self.assertIsNone(_parse_api_timestamp(0))
        self.assertIsNone(_parse_api_timestamp(None))
        self.assertIsNone(_parse_api_timestamp(""))

    def test_customer_replied_in_window(self):
        inside = datetime(2026, 7, 14, 9, 0, 0)
        contact = {"user_last_reply_time": int(inside.timestamp() * 1000)}
        self.assertTrue(customer_replied_in_window(contact, self.window))

    def test_customer_replied_outside_window(self):
        outside = datetime(2026, 7, 12, 9, 0, 0)
        contact = {"user_last_reply_time": int(outside.timestamp() * 1000)}
        self.assertFalse(customer_replied_in_window(contact, self.window))

    def test_filter_contacts(self):
        inside = datetime(2026, 7, 14, 9, 0, 0)
        outside = datetime(2026, 7, 12, 9, 0, 0)
        contacts = {
            "a": {"user_last_reply_time": int(inside.timestamp() * 1000)},
            "b": {"user_last_reply_time": int(outside.timestamp() * 1000)},
            "c": {"user_last_reply_time": 0},
        }
        kept, skipped = _filter_contacts_by_user_last_reply(contacts, self.window)
        self.assertEqual(set(kept.keys()), {"a"})
        self.assertEqual(skipped, 2)


if __name__ == "__main__":
    unittest.main()
