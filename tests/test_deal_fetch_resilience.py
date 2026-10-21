import sys
from pathlib import Path
from datetime import datetime
from contextlib import ExitStack
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"智能体"))
import fetch_deal_salesmartly as fetch

class TestDealFetchRecovery(unittest.TestCase):
    def run_fetch(self,sessions):
        day=datetime(2026,10,20)
        contacts={str(i):{"chat_user_id":str(i),"session_id":"s"+str(i),"name":"Customer"+str(i),"labels":"全款","sys_user_id":9,"channel":1} for i in (1,2)}
        messages={"s"+str(i):[{"sender_type":1,"text":"Current customer message "+str(i),"send_time":1792454400}] for i in (1,2)}
        with ExitStack() as stack:
            stack.enter_context(patch.object(fetch,"build_tag_value_id_map",return_value={fetch.date_tag_name(day):"label-id"}))
            stack.enter_context(patch.object(fetch,"_fetch_contacts_by_tag_value",return_value=contacts))
            stack.enter_context(patch.object(fetch,"_fetch_sessions_by_ids",return_value=sessions))
            get_messages=stack.enter_context(patch.object(fetch,"_fetch_messages_for_sessions",return_value=messages))
            stack.enter_context(patch.object(fetch,"fetch_members",return_value=[]))
            df,meta=fetch.fetch_yesterday_deal_dataframe(Mock(config=object()),target_day=day)
            self.assertEqual(set(get_messages.call_args.args[1]),{"s1","s2"})
            return df,meta

    def test_missing_one_detail_still_imports_both_customer_histories(self):
        df,meta=self.run_fetch([{"session_id":"s1"}])
        self.assertEqual(len(df),2)
        self.assertEqual(meta["session_details_missing"],1)
        self.assertIn("Current customer message 2",df.loc[df["会话ID"]=="s2","会话消息内容"].iloc[0])

    def test_all_details_empty_uses_contact_metadata_and_keeps_histories(self):
        df,meta=self.run_fetch([])
        self.assertEqual(len(df),2)
        self.assertEqual(set(df["接待成员"]),{"9"})
        self.assertEqual(meta["session_details_missing"],2)

    def test_detail_request_error_propagates_for_nightly_retry(self):
        client=Mock()
        client.get.side_effect=RuntimeError("temporary API failure")
        with patch.object(fetch,"SaleSmartlyClient",return_value=client):
            with self.assertRaisesRegex(RuntimeError,"temporary API failure"):
                fetch._fetch_sessions_by_ids(object(),["s1"])

if __name__=="__main__":unittest.main()
