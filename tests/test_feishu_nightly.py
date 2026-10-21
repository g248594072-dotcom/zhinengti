import json
from pathlib import Path
from contextlib import ExitStack
from datetime import date
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"智能体"))
import feishu_notify as feishu
import nightly_pipeline as nightly

class TestNightlyReport(unittest.TestCase):
    def test_combined_report_includes_import_learning_repairs_and_errors(self):
        report=feishu.build_nightly_pipeline_report({"business_day":"2026-10-21","status":"partial_failure","steps":{
            "deal":{"imports":[{"day":"2026-10-21","ok":True,"imported":3,"unchanged":2,"customers_created":1,"messages_inserted":17}],"learning":{"total":5,"success":4,"failed":1,"repair_attempted":2,"repair_remaining":8},"kb_pending":9},
            "delete":{"imports":[{"day":"2026-10-21","ok":False}],"learning":{}}},"errors":["temporary API failure"]})
        for value in ("成交客户","删除客户","入库成功 3","跳过 2","新增消息 17","成功 4","失败 1","本次尝试 2","尚待 8","知识库：9 组","temporary API failure","至少7条"):
            self.assertIn(value,report)
        self.assertIn("未完成",report)

    def test_robot_error_code_is_not_reported_as_success_without_message(self):
        response=Mock(status_code=200)
        response.json.return_value={"code":19024}
        with patch.object(feishu.requests,"post",return_value=response):
            self.assertFalse(feishu.send_feishu_text("report","https://example.invalid/robot"))

    def test_feishu_success_response_is_recognized(self):
        response=Mock(status_code=200)
        response.json.return_value={"code":0,"msg":"success"}
        with patch.object(feishu.requests,"post",return_value=response) as post:
            self.assertTrue(feishu.send_feishu_text("report","https://example.invalid/robot"))
            self.assertEqual(post.call_args.kwargs["json"]["content"]["text"],"report")

    def test_notification_failure_preserves_success_and_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"state.json"
            result={"status":"success"}; state={"last_run":result}
            with patch.object(feishu,"get_feishu_webhook_url",return_value="configured"),patch.object(feishu,"notify_nightly_pipeline_result",side_effect=RuntimeError("network down")) as notify:
                self.assertIs(nightly._finish_run(path,state,result),result)
                notify.assert_called_once()
            saved=json.loads(path.read_text())
            self.assertEqual(saved["last_run"]["status"],"success")
            self.assertEqual(saved["last_run"]["notification"]["status"],"failed")

    def test_database_failure_is_broadcast_and_saved_once(self):
        import db
        with tempfile.TemporaryDirectory() as directory, patch.object(db,"init_db",side_effect=RuntimeError("db down")),patch.object(db,"format_database_error",return_value="数据库不可达"),patch.object(feishu,"get_feishu_webhook_url",return_value="configured"),patch.object(feishu,"notify_nightly_pipeline_result",return_value=True) as notify:
            result=nightly.run_pipeline(target_day=date(2026,10,21),config_dir=directory)
            notify.assert_called_once_with(result)
            self.assertEqual(result["notification"]["status"],"sent")
            self.assertEqual(result["status"],"partial_failure")

    def test_successful_pipeline_broadcasts_once_after_both_types_finish(self):
        import db,qc_core,fetch_deal_daily,fetch_delete_daily,deal_intelligence,delete_intelligence
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            stack.enter_context(patch.object(db,"init_db"))
            stack.enter_context(patch.object(qc_core,"load_config_from_disk",return_value=({"api_key":"configured-test"},"")))
            for module,name in ((fetch_deal_daily,"run_fetch_deal_daily"),(fetch_delete_daily,"run_fetch_delete_daily")):
                stack.enter_context(patch.object(module,name,return_value={"ok":True,"summary":{"settlement":{"success_count":2}}}))
            for module,name in ((db,"get_unanalyzed_deal_customers"),(db,"get_unanalyzed_delete_customers")):
                stack.enter_context(patch.object(module,name,return_value=[]))
            for module,name in ((deal_intelligence,"analyze_unlearned_deals"),(delete_intelligence,"analyze_unlearned_deletes")):
                stack.enter_context(patch.object(module,name,return_value={"total":0,"success":0,"failed":0}))
            stack.enter_context(patch("weekly_prompt_merge.count_rules_not_in_supplement",return_value={"pending":2}))
            stack.enter_context(patch("weekly_prompt_merge.count_delete_rules_not_in_supplement",return_value={"pending":3}))
            stack.enter_context(patch.object(feishu,"get_feishu_webhook_url",return_value="configured"))
            notify=stack.enter_context(patch.object(feishu,"notify_nightly_pipeline_result",return_value=True))
            result=nightly.run_pipeline(target_day=date(2026,10,21),config_dir=directory)
            notify.assert_called_once_with(result)
            self.assertEqual(set(result["steps"]),{"deal","delete"})
            self.assertEqual(result["steps"]["deal"]["imports"][0]["imported"],2)
            self.assertEqual(result["steps"]["delete"]["kb_pending"],3)
            self.assertEqual(result["notification"]["status"],"sent")

if __name__=="__main__":unittest.main()
