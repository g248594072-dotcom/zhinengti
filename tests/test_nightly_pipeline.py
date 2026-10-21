# -*- coding: utf-8 -*-
import importlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import ExitStack
from sqlalchemy import create_engine, text

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"智能体"))
import db
import nightly_pipeline as nightly


class TestIndependentLearning(unittest.TestCase):
    def test_legacy_skip_still_calls_ai_for_current_customer(self):
        for kind in ("deal","delete"):
            module=importlib.import_module(kind+"_intelligence")
            with self.subTest(kind=kind), ExitStack() as stack:
                stack.enter_context(patch.object(module,"get_customer_full_dialog",return_value="CURRENT_CUSTOMER_UNIQUE_CHAT"))
                stack.enter_context(patch.object(module,"assess_"+kind+"_learning_need",return_value={"action":"skip","best_match":{"customer_profile":"WRONG_OLD_PROFILE"},"ref_compact":"OLD_REFERENCE_ONLY"}))
                call=stack.enter_context(patch.object(module.core,"call_llm_prompt",return_value={"客户画像":"CURRENT_PROFILE","智能体判断规则":"CURRENT_RULES"}))
                save=stack.enter_context(patch.object(module,"save_"+kind+"_analysis"))
                result=getattr(module,"analyze_"+kind+"_customer")({"customer_id":101,"session_id":3},{})
                call.assert_called_once()
                self.assertIn("CURRENT_CUSTOMER_UNIQUE_CHAT",call.call_args.args[2])
                self.assertEqual(result["客户画像"],"CURRENT_PROFILE")
                self.assertEqual(result["_学习模式"],"增量更新")
                save.assert_called_once()

    def test_empty_ai_result_preserves_old_analysis_for_retry(self):
        for kind in ("deal","delete"):
            module=importlib.import_module(kind+"_intelligence")
            with self.subTest(kind=kind), ExitStack() as stack:
                stack.enter_context(patch.object(module,"get_customer_full_dialog",return_value="current dialog"))
                stack.enter_context(patch.object(module,"assess_"+kind+"_learning_need",return_value={"action":"full"}))
                stack.enter_context(patch.object(module.core,"call_llm_prompt",return_value={"客户画像":"incomplete"}))
                save=stack.enter_context(patch.object(module,"save_"+kind+"_analysis"))
                result=getattr(module,"analyze_"+kind+"_customer")({"customer_id":101},{})
                self.assertIn("_错误",result)
                save.assert_not_called()

    def test_high_similarity_does_not_skip_current_customer(self):
        for kind in ("deal","delete"):
            coverage=importlib.import_module(kind+"_coverage")
            context=importlib.import_module(kind+"_context")
            with self.subTest(kind=kind), ExitStack() as stack:
                stack.enter_context(patch.object(context,"find_similar_"+("deals" if kind=="deal" else "deletes"),return_value=[{"_match_score":99,"recommended_agent_rules":"OLD_RULES"}]))
                stack.enter_context(patch.object(coverage,"load_supplement_excerpt" if kind=="deal" else "load_delete_supplement_excerpt",return_value=""))
                if kind=="delete":
                    stack.enter_context(patch.object(coverage,"build_deal_comparison_note",return_value=""))
                result=getattr(coverage,"assess_"+kind+"_learning_need")("current chat",{})
                self.assertEqual(result["action"],"incremental")


class TestStructuredAnalysisFields(unittest.TestCase):
    def test_structured_ai_fields_bind_to_database_without_losing_contents(self):
        engine=create_engine("sqlite://")
        value=["当前客户原话", {"证据": "聊天时间点"}]
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE analysis(value TEXT)"))
            conn.execute(text("INSERT INTO analysis VALUES(:value)"), {"value":db.analysis_text(value)})
            saved=conn.execute(text("SELECT value FROM analysis")).scalar_one()
            self.assertEqual(json.loads(saved),value)
        engine.dispose()


class TestPendingQueries(unittest.TestCase):
    def test_actual_query_selects_inherited_blank_and_changed_but_not_unchanged(self):
        engine=create_engine("sqlite://")
        with engine.connect() as conn:
            conn.connection.driver_connection.create_function("JSON_UNQUOTE",1,lambda value:value)
            conn.execute(text("CREATE TABLE customers(id INTEGER, status TEXT, deal_at TEXT, deleted_at TEXT, original_session_id TEXT, contact_name TEXT,sales_name TEXT)"))
            conn.execute(text("CREATE TABLE chat_sessions(id INTEGER,customer_id INTEGER,updated_at TEXT,message_count_customer INTEGER)"))
            for kind,status in (("deal","已成交"),("delete","已删除")):
                conn.execute(text(f"CREATE TABLE {kind}_analysis(id INTEGER,customer_id INTEGER,analyzed_at TEXT,raw_result TEXT,recommended_agent_rules TEXT)"))
                for i in range(1,7):
                    cid=i+(0 if kind=="deal" else 100)
                    conn.execute(text("INSERT INTO customers VALUES(:id,:status,'2026-10-01','2026-10-01','','','')"),{"id":cid,"status":status})
                    conn.execute(text("INSERT INTO chat_sessions VALUES(:id,:id,:updated,:n)"),{"id":cid,"updated":"2026-10-03" if i==4 else "2026-10-01","n":6 if i==6 else 7})
                    if i!=1:
                        conn.execute(text(f"INSERT INTO {kind}_analysis VALUES(:id,:id,'2026-10-02',:raw,:rules)"),{"id":cid,"raw":json.dumps({"_学习模式":"继承跳过" if i==2 else "全量复盘"},ensure_ascii=False),"rules":"" if i==5 else "rules"})
                rows=conn.execute(text(db._pending_learning_query(kind)),{"lim":100}).mappings().all()
                self.assertEqual({r["customer_id"] for r in rows},{i+(0 if kind=="deal" else 100) for i in (1,2,4,5)})
                self.assertEqual(next(r for r in rows if r["needs_repair"])["customer_id"],2+(0 if kind=="deal" else 100))
        engine.dispose()


class TestNightlyPipeline(unittest.TestCase):
    def test_business_day_uses_beijing_even_on_utc_host(self):
        now=datetime(2026,10,21,18,0,tzinfo=timezone.utc)
        self.assertEqual(nightly.previous_day(now),date(2026,10,21))

    def test_missed_import_days_are_caught_up(self):
        self.assertEqual(nightly.days_to_import("2026-10-18",date(2026,10,21)),[date(2026,10,d) for d in (19,20,21)])
        self.assertEqual(nightly.days_to_import(None,date(2026,10,21)),[date(2026,10,21)])
        self.assertEqual(nightly.days_to_import("2026-10-21",date(2026,10,21)),[])

    def test_new_customers_are_not_capped_by_historical_repair_limit(self):
        fresh=[{"customer_id":i} for i in range(100)]
        old=[{"customer_id":i+100,"needs_repair":1} for i in range(80)]
        chosen,remaining=nightly.select_learning_customers(old+fresh,50)
        self.assertEqual(chosen[:100],fresh)
        self.assertEqual(len(chosen),150)
        self.assertEqual(remaining,80)

    def test_lock_prevents_parallel_runs_and_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                with nightly.pipeline_lock(directory):
                    with self.assertRaises(OSError):
                        with nightly.pipeline_lock(directory):pass
                    raise RuntimeError("simulated crash")
            with nightly.pipeline_lock(directory):pass

    def test_import_failure_does_not_advance_day_or_block_other_type(self):
        import qc_core, fetch_deal_daily, fetch_delete_daily, deal_intelligence, delete_intelligence
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.object(db,"init_db"))
            fetch_deal=stack.enter_context(patch.object(fetch_deal_daily,"run_fetch_deal_daily",return_value={"ok":False,"error":"API failure"}))
            fetch_delete=stack.enter_context(patch.object(fetch_delete_daily,"run_fetch_delete_daily",return_value={"ok":True,"summary":{}}))
            stack.enter_context(patch.object(db,"get_unanalyzed_deal_customers",return_value=[]))
            stack.enter_context(patch.object(db,"get_unanalyzed_delete_customers",return_value=[{"customer_id":1}]))
            stack.enter_context(patch.object(qc_core,"load_config_from_disk",return_value=({"api_key":"configured-test"},"")))
            stack.enter_context(patch.object(deal_intelligence,"analyze_unlearned_deals",return_value={"failed":0}))
            analyze=stack.enter_context(patch.object(delete_intelligence,"analyze_unlearned_deletes",return_value={"total":1,"success":1,"failed":0}))
            result=nightly.run_pipeline(target_day=date(2026,10,20),config_dir=directory)
            state=json.loads((Path(directory)/"nightly_pipeline_state.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"],"partial_failure")
            self.assertNotIn("deal",state["last_imported_day"])
            self.assertEqual(state["last_imported_day"]["delete"],"2026-10-20")
            analyze.assert_called_once()
            fetch_deal.assert_called_once()
            fetch_delete.assert_called_once()


if __name__=="__main__":unittest.main()
