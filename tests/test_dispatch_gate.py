import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import fatigue_decision, parse_time
from src.service import Service

EPOCH = datetime(2026, 9, 26, 20, 0, 0, tzinfo=timezone.utc)
# 补录的班次都发生在EPOCH当天；时钟从第二天开始，保证历史时刻都早于"现在"
START = EPOCH + timedelta(days=1)


def iso(moment):
    return moment.replace(microsecond=0).isoformat()


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, hours):
        self.now += timedelta(hours=hours)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "gate.db"))
        self.clock = Clock()
        self.service = Service(self.repo, clock=self.clock)
        self.disp = self.service.dispatch
        self.item = self.service.create_item(
            {"title": "东线火线", "description": "夜班轮换", "severity": "high",
             "quantity": 5, "threshold": 10, "external_ref": "GATE-1"},
            "creator", "field_commander")
        self.item2 = self.service.create_item(
            {"title": "西线火线", "description": "夜班轮换", "severity": "high",
             "quantity": 5, "threshold": 10, "external_ref": "GATE-2"},
            "creator", "field_commander")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _shift(self, code, start_h, end_h, source="backfill", item_id=None):
        return self.disp.backfill_attendance(
            {"member_code": code, "arrival_at": iso(EPOCH + timedelta(hours=start_h)),
             "depart_at": iso(EPOCH + timedelta(hours=end_h)),
             "item_id": item_id or self.item["id"]},
            "recorder", "field_commander")

    # 纯规则
    def test_fresh_member_eligible_with_eight_hours(self):
        result = fatigue_decision([], EPOCH, now=EPOCH)
        self.assertEqual(result["decision"], "eligible")
        self.assertEqual(result["available_hours"], 8.0)

    def test_nine_hour_shift_held_before_two_hour_rest(self):
        sessions = [{"arrival_at": iso(EPOCH), "depart_at": iso(EPOCH + timedelta(hours=9))}]
        # 仅休息1小时：连续9小时超限1小时
        result = fatigue_decision(sessions, EPOCH + timedelta(hours=10),
                                  now=EPOCH + timedelta(hours=10))
        self.assertEqual(result["decision"], "held")
        self.assertEqual(result["overtime_hours"], 1.0)
        self.assertEqual(result["rest_short_hours"], 1.0)
        self.assertEqual(result["available_hours"], 0.0)
        self.assertEqual(result["eligible_at"], iso(EPOCH + timedelta(hours=11)))
        # 满2小时休息后链条重置，可派工时恢复8小时
        recovered = fatigue_decision(sessions, EPOCH + timedelta(hours=11),
                                     now=EPOCH + timedelta(hours=11))
        self.assertEqual(recovered["decision"], "eligible")
        self.assertEqual(recovered["available_hours"], 8.0)

    def test_rest_under_two_hours_held_even_within_eight(self):
        sessions = [{"arrival_at": iso(EPOCH), "depart_at": iso(EPOCH + timedelta(hours=7))}]
        result = fatigue_decision(sessions, EPOCH + timedelta(hours=8),
                                  now=EPOCH + timedelta(hours=8))
        self.assertEqual(result["decision"], "held")
        self.assertEqual(result["overtime_hours"], 0.0)
        self.assertEqual(result["rest_short_hours"], 1.0)
        self.assertEqual(result["available_hours"], 1.0)

    def test_two_hour_rest_resets_chain(self):
        sessions = [{"arrival_at": iso(EPOCH), "depart_at": iso(EPOCH + timedelta(hours=9))}]
        result = fatigue_decision(sessions, EPOCH + timedelta(hours=11),
                                  now=EPOCH + timedelta(hours=11))
        self.assertEqual(result["decision"], "eligible")
        self.assertEqual(result["continuous_hours"], 0.0)

    def test_chained_shifts_accumulate_continuous_hours(self):
        sessions = [
            {"arrival_at": iso(EPOCH), "depart_at": iso(EPOCH + timedelta(hours=7))},
            {"arrival_at": iso(EPOCH + timedelta(hours=8)),
             "depart_at": iso(EPOCH + timedelta(hours=12))},
        ]
        result = fatigue_decision(sessions, EPOCH + timedelta(hours=12),
                                  now=EPOCH + timedelta(hours=12))
        # 两班只隔1小时，连续作业=11小时，超限3小时
        self.assertEqual(result["decision"], "held")
        self.assertEqual(result["continuous_hours"], 12.0)
        self.assertEqual(result["overtime_hours"], 4.0)

    # 到场离场
    def test_duplicate_check_in_keeps_first_result(self):
        first = self.disp.check_in(
            {"member_code": "M-1", "member_name": "张三", "item_id": self.item["id"]},
            "recorder", "field_commander")
        second = self.disp.check_in(
            {"member_code": "M-1", "member_name": "张三改名", "item_id": self.item2["id"]},
            "recorder", "field_commander")
        self.assertTrue(second["deduped"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["item_id"], self.item["id"])
        self.assertEqual(len(self.disp.list_attendance("M-1", "viewer")), 1)

    def test_check_out_then_held_dispatch(self):
        self.disp.check_in({"member_code": "M-2"}, "recorder", "field_commander")
        self.clock.advance(9)
        self.disp.check_out({"member_code": "M-2", "depart_at": iso(self.clock.now)},
                            "recorder", "field_commander")
        self.clock.advance(1)
        dispatch = self.disp.create_dispatch(
            {"member_code": "M-2", "item_id": self.item2["id"]},
            "dispatcher", "logistics")
        self.assertEqual(dispatch["status"], "held")
        gate = dispatch["gate_detail"]
        self.assertEqual(gate["overtime_hours"], 1.0)
        self.assertEqual(gate["available_hours"], 0.0)

    def test_cannot_dispatch_member_still_on_duty(self):
        self.disp.check_in({"member_code": "M-3", "item_id": self.item["id"]},
                           "recorder", "field_commander")
        dispatch = self.disp.create_dispatch(
            {"member_code": "M-3", "item_id": self.item2["id"]},
            "dispatcher", "logistics")
        self.assertEqual(dispatch["status"], "held")
        self.assertTrue(dispatch["gate_detail"]["on_duty"])

    # 迟到补录改变资格 -> 待派重新判定，现场记录仍可查
    def test_backfill_reevaluates_held_dispatch(self):
        # 10小时前到场（当时实时登记），离场漏记；1小时前才实际离场（连续9小时）
        arrival = self.repo.add_attendance(
            "M-4", "李四", self.item["id"],
            iso(self.clock.now - timedelta(hours=10)), None, "realtime", "recorder")
        backfilled = self.disp.backfill_attendance(
            {"member_code": "M-4", "arrival_at": arrival["arrival_at"],
             "depart_at": iso(self.clock.now - timedelta(hours=1)),
             "item_id": self.item["id"]},
            "recorder", "field_commander")
        self.assertEqual(backfilled["id"], arrival["id"])
        self.assertEqual(backfilled["source"], "backfill")
        # 此时新派工：连续9小时超限1小时，且只休息1小时 -> 待休整
        held = self.disp.create_dispatch(
            {"member_code": "M-4", "item_id": self.item2["id"]},
            "dispatcher", "logistics")
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["gate_detail"]["overtime_hours"], 1.0)
        self.assertEqual(held["gate_detail"]["rest_short_hours"], 1.0)
        # 复核确认：队员其实3小时前就已离场（实际只作业7小时，已休息3小时>=2）
        # 迟到补录更正离场时刻后，资格翻转，待派记录自动重新判定为已派入
        corrected = self.disp.backfill_attendance(
            {"member_code": "M-4", "arrival_at": arrival["arrival_at"],
             "depart_at": iso(self.clock.now - timedelta(hours=3)),
             "item_id": self.item["id"]},
            "recorder", "field_commander")
        self.assertEqual(corrected["id"], arrival["id"])
        released = self.repo.get_dispatch(held["id"])
        self.assertEqual(released["status"], "assigned")
        self.assertTrue(released["reevaluated"])
        self.assertEqual(released["gate_detail"]["decision"], "eligible")
        # 现场记录始终可查：仍是同一条到场记录，审计链完整
        records = self.disp.list_attendance("M-4", "viewer")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["depart_at"],
                         iso(self.clock.now - timedelta(hours=3)))
        self.assertTrue(self.repo.verify_audit_chain())

    def test_backfill_new_closed_session_when_no_open_record(self):
        # 无未离场记录时补录一整段班次，正常新增
        record = self._shift("M-4B", 0, 6)
        self.assertEqual(record["source"], "backfill")
        self.assertEqual(len(self.disp.list_attendance("M-4B", "viewer")), 1)

    def test_overlapping_backfill_rejected(self):
        self._shift("M-5", 0, 5)
        with self.assertRaises(ConflictError):
            self._shift("M-5", 4, 6)

    def test_backfill_depart_before_arrive_rejected(self):
        with self.assertRaises(ValidationError):
            self._shift("M-6", 6, 4)

    # 权限
    def test_roles_enforced(self):
        with self.assertRaises(PermissionDenied):
            self.disp.create_dispatch(
                {"member_code": "M-7", "item_id": self.item["id"]},
                "x", "viewer")
        with self.assertRaises(PermissionDenied):
            self.disp.check_in({"member_code": "M-7"}, "x", "viewer")

    def test_dispatch_into_closed_item_rejected(self):
        from src.rules import STATES, TRANSITION_ROLES
        item = self.service.get_item(self.item["id"], "viewer")
        self.service.add_record(item["id"],
                                {"kind": "ok", "detail": "closed note",
                                 "status": "closed", "external_ref": "C-1"},
                                "r", "field_commander")
        for target in STATES[1:]:
            item = self.service.transition(
                item["id"], target, item["version"], "ic",
                TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError):
            self.disp.create_dispatch(
                {"member_code": "M-8", "item_id": self.item["id"]},
                "dispatcher", "logistics")

    def test_late_check_in_must_use_backfill(self):
        self.clock.advance(3)
        with self.assertRaises(ValidationError):
            self.disp.check_in(
                {"member_code": "M-9",
                 "arrival_at": iso(EPOCH)}, "recorder", "field_commander")


if __name__ == "__main__":
    unittest.main()
