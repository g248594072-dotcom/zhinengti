import sys
from pathlib import Path
from datetime import datetime
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"智能体"))
import db

class TestIncrementalMessageImport(unittest.TestCase):
    def message(self,day,content="Thanks"):
        return {"sender_name":"Agent","sender_type":"sales","content":content,
                "sent_at":datetime(2026,10,day,12),"message_order":1}

    def test_same_words_on_different_day_are_new_messages(self):
        old=self.message(20)
        new=self.message(21)
        self.assertEqual(db.find_message_position_in_list([old],new),-1)
        self.assertEqual(db.find_message_position_in_list([old,new],old),0)

    def test_datetime_and_iso_timestamp_match_same_message(self):
        old=self.message(20)
        incoming=dict(old,sent_at="2026-10-20T12:00:00")
        self.assertEqual(db.find_message_position_in_list([old],incoming),0)

    def test_unmatched_history_records_conflict_instead_of_success(self):
        with patch.object(db,"get_session_messages",return_value=[self.message(20)]), \
             patch.object(db,"record_import_conflict") as record, \
             patch.object(db,"get_engine") as engine:
            result=db.append_new_messages_by_last_line(1,2,"key","session",[self.message(21,"different")],raw_dialog="new history")
            self.assertTrue(result["conflict"])
            record.assert_called_once()
            engine.assert_not_called()

    def test_conflict_is_reported_as_failed_import_for_retry(self):
        with patch.object(db,"init_db"), \
             patch.object(db,"get_or_create_customer_by_session_id",return_value={"id":1}), \
             patch.object(db,"get_or_create_chat_session",return_value={"id":2}), \
             patch.object(db,"append_new_messages_by_last_line",return_value={"conflict":True,"skipped":True,"reason":"history mismatch"}):
            result=db.upsert_sessions([{"会话ID":"s1","联系人":"customer","对话":"客户：hello"}])
            self.assertEqual(result["conflicts"],1)
            self.assertEqual(result["session_details"][0]["status"],"失败")

if __name__=="__main__":unittest.main()
