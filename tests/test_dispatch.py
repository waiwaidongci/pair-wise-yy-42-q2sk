import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path

from src import fatigue
from src.dispatch import DispatchService, STATUS_ASSIGNED, STATUS_PENDING
from src.domain import NotFoundError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


def hours_ago(moment, hours):
    return fatigue.format_moment(moment - timedelta(hours=hours))


class FatigueRulesTest(unittest.TestCase):
    def test_parse_and_format_moment(self):
        moment = fatigue.parse_moment("2026-09-26T08:00:00Z")
        self.assertEqual(moment.tzinfo, timezone.utc)
        naive = fatigue.parse_moment("2026-09-26 08:00:00")
        self.assertEqual(naive.utcoffset(), timedelta(0))
        self.assertEqual(fatigue.parse_moment(fatigue.format_moment(moment)), moment)
        with self.assertRaises(ValidationError):
            fatigue.parse_moment("not-a-time")

    def test_evaluate_without_records_is_eligible(self):
        result = fatigue.evaluate([], fatigue.utc_moment())
        self.assertTrue(result["eligible"])
        self.assertEqual(result["work_hours"], 0.0)
        self.assertIsNone(result["rest_hours"])

    def test_evaluate_on_site_overwork_blocks_without_available(self):
        now = fatigue.utc_moment()
        events = [{"id": 1, "kind": "arrival",
                   "happened_at": now - timedelta(hours=9)}]
        result = fatigue.evaluate(events, now)
        self.assertFalse(result["eligible"])
        self.assertTrue(result["on_site"])
        self.assertIsNone(result["available_at"])
        self.assertEqual(result["reasons"][0]["rule"], "continuous_work")
        self.assertAlmostEqual(result["reasons"][0]["exceeded_hours"], 1.0, places=1)
        self.assertIsNotNone(result["note"])


class DispatchGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.dispatch = DispatchService(self.repo)
        self.now = fatigue.utc_moment()

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def attendance(self, member, kind, hours, actor="logger", role="logistics"):
        return self.dispatch.record_attendance(
            {"member": member, "kind": kind,
             "happened_at": hours_ago(self.now, hours)}, actor, role)

    def dispatch_member(self, member, actor="commander",
                        role="field_commander", **extra):
        payload = {"member": member}
        payload.update(extra)
        return self.dispatch.create_dispatch(payload, actor, role)

    def test_attendance_records_moments_and_repeat_arrival_reused(self):
        first = self.attendance("张三", "arrival", 6)
        self.assertFalse(first["reused"])
        again = self.attendance("张三", "arrival", 5)
        self.assertTrue(again["reused"])
        self.assertEqual(again["record"]["id"], first["record"]["id"])
        self.assertEqual(again["record"]["happened_at"],
                         first["record"]["happened_at"])
        self.attendance("张三", "departure", 1)
        third = self.attendance("张三", "arrival", 0.2)
        self.assertFalse(third["reused"])
        records = self.dispatch.list_attendance("张三", "viewer")
        self.assertEqual([r["kind"] for r in records],
                         ["arrival", "departure", "arrival"])

    def test_dispatch_assigned_when_rested_or_fresh(self):
        self.attendance("李四", "arrival", 7)
        self.attendance("李四", "departure", 3)
        record = self.dispatch_member("李四")
        self.assertEqual(record["status"], STATUS_ASSIGNED)
        self.assertTrue(record["evaluation"]["eligible"])
        fresh = self.dispatch_member("新人")
        self.assertEqual(fresh["status"], STATUS_ASSIGNED)

    def test_overwork_and_short_rest_go_pending_with_details(self):
        self.attendance("王五", "arrival", 9.5)
        self.attendance("王五", "departure", 0.5)
        record = self.dispatch_member("王五")
        self.assertEqual(record["status"], STATUS_PENDING)
        evaluation = record["evaluation"]
        rules = {r["rule"]: r for r in evaluation["reasons"]}
        self.assertAlmostEqual(rules["continuous_work"]["exceeded_hours"], 1.0, places=1)
        self.assertAlmostEqual(rules["rest"]["exceeded_hours"], 1.5, places=1)
        expected_available = fatigue.format_moment(
            self.now - timedelta(hours=0.5) + timedelta(hours=2))
        self.assertEqual(evaluation["available_at"], expected_available)

    def test_short_rest_alone_goes_pending(self):
        self.attendance("赵六", "arrival", 6)
        self.attendance("赵六", "departure", 1)
        record = self.dispatch_member("赵六")
        self.assertEqual(record["status"], STATUS_PENDING)
        evaluation = record["evaluation"]
        self.assertEqual([r["rule"] for r in evaluation["reasons"]], ["rest"])
        self.assertAlmostEqual(evaluation["reasons"][0]["exceeded_hours"], 1.0, places=1)
        self.assertIsNotNone(evaluation["available_at"])

    def test_late_backfill_reevaluates_pending_and_records_remain(self):
        self.attendance("孙七", "arrival", 10)
        pending = self.dispatch_member("孙七")
        self.assertEqual(pending["status"], STATUS_PENDING)
        self.assertIsNone(pending["evaluation"]["available_at"])
        result = self.attendance("孙七", "departure", 3)
        changed = result["reevaluated"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["id"], pending["id"])
        self.assertEqual(changed[0]["status"], STATUS_ASSIGNED)
        archive = self.dispatch.list_dispatches("viewer", member="孙七")
        self.assertEqual(archive[0]["status"], STATUS_ASSIGNED)
        records = self.dispatch.list_attendance("孙七", "viewer")
        self.assertEqual([r["kind"] for r in records], ["arrival", "departure"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_backfill_without_release_keeps_pending_and_updates_detail(self):
        self.attendance("吴九", "arrival", 6)
        self.attendance("吴九", "departure", 0.5)
        pending = self.dispatch_member("吴九")
        self.assertEqual(pending["status"], STATUS_PENDING)
        result = self.attendance("吴九", "arrival", 4)
        self.assertEqual(len(result["reevaluated"]), 1)
        updated = result["reevaluated"][0]
        self.assertEqual(updated["id"], pending["id"])
        self.assertEqual(updated["status"], STATUS_PENDING)
        self.assertAlmostEqual(updated["evaluation"]["work_hours"], 3.5, places=1)

    def test_out_of_order_backfill_is_flagged(self):
        self.attendance("周八", "arrival", 8)
        self.attendance("周八", "departure", 1)
        late = self.attendance("周八", "arrival", 5)
        self.assertTrue(late["record"]["backfill"])
        records = self.dispatch.list_attendance("周八", "viewer")
        self.assertEqual(sum(1 for r in records if r["backfill"]), 1)

    def test_dispatch_links_existing_item(self):
        item = self.service.create_item(
            {"title": "任务区A", "description": "火线东侧", "severity": "high",
             "quantity": 5, "threshold": 10}, "creator", "field_commander")
        record = self.dispatch_member("郑十", item_id=item["id"])
        self.assertEqual(record["item_id"], item["id"])
        self.assertEqual(record["status"], STATUS_ASSIGNED)

    def test_fatigue_preview(self):
        self.attendance("钱二", "arrival", 3)
        preview = self.dispatch.fatigue_preview("钱二", None, "viewer")
        self.assertTrue(preview["evaluation"]["eligible"])
        self.assertTrue(preview["evaluation"]["on_site"])

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.dispatch.record_attendance(
                {"member": "张三", "kind": "arrival"}, "x", "viewer")
        with self.assertRaises(PermissionDenied):
            self.dispatch.create_dispatch({"member": "张三"}, "x", "logistics")
        with self.assertRaises(PermissionDenied):
            self.dispatch.list_dispatches("unknown_role")

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.dispatch.record_attendance(
                {"member": "张三", "kind": "break"}, "x", "logistics")
        with self.assertRaises(ValidationError):
            self.attendance("张三", "arrival", -2)
        with self.assertRaises(ValidationError):
            self.dispatch.create_dispatch(
                {"member": "张三", "item_id": "abc"}, "x", "field_commander")
        with self.assertRaises(NotFoundError):
            self.dispatch.create_dispatch(
                {"member": "张三", "item_id": 999}, "x", "field_commander")
        with self.assertRaises(ValidationError):
            self.dispatch.list_dispatches("viewer", status="unknown")


if __name__ == "__main__":
    unittest.main()
