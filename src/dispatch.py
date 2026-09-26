"""派工留档：到场/离场登记、派工门禁判定、迟到补录后待派记录重新判定。

- 到场和离场都记时刻，同一队员重复到场沿用首次结果；
- 新派工按最近一次离场做疲劳判定，超限只进待休整并留档；
- 迟到补录改变资格时，该队员的待派记录按当前记录重新判定；
- 现场记录（attendance）与派工留档（dispatches）都可查询。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import fatigue
from .audit import utc_now
from .domain import ValidationError, ensure_role, require_text
from .repository import Repository

ATTENDANCE_KINDS = ("arrival", "departure")
STATUS_ASSIGNED = "assigned"
STATUS_PENDING = "pending_rest"
DISPATCH_STATUSES = (STATUS_ASSIGNED, STATUS_PENDING)
ATTENDANCE_ROLES = {"field_commander", "logistics"}
DISPATCH_ROLES = {"field_commander", "incident_commander"}
VIEW_ROLES = {"field_commander", "incident_commander", "logistics", "viewer"}


class DispatchRepository:
    """到场与派工记录的SQLite留档，复用主仓库的连接、锁和审计链。"""

    def __init__(self, repository: Repository):
        self._repo = repository
        with repository._lock, repository.conn:
            repository.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('arrival','departure')),
                    happened_at TEXT NOT NULL,
                    backfill INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_attendance_member
                    ON attendance(member, happened_at);
                CREATE TABLE IF NOT EXISTS dispatches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member TEXT NOT NULL,
                    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
                    status TEXT NOT NULL CHECK(status IN ('assigned','pending_rest')),
                    requested_at TEXT NOT NULL,
                    evaluation TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_dispatches_member
                    ON dispatches(member, status);
                """
            )

    @staticmethod
    def _attendance_row(row) -> Dict[str, Any]:
        item = dict(row)
        item["backfill"] = bool(item["backfill"])
        return item

    @staticmethod
    def _dispatch_row(row) -> Dict[str, Any]:
        item = dict(row)
        item["evaluation"] = json.loads(item["evaluation"])
        return item

    def insert_attendance(self, member: str, kind: str, happened_at: str,
                          backfill: bool, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._repo._lock, self._repo.conn:
            cur = self._repo.conn.execute(
                """INSERT INTO attendance(member, kind, happened_at, backfill,
                   created_by, created_at) VALUES(?,?,?,?,?,?)""",
                (member, kind, happened_at, 1 if backfill else 0, actor, now),
            )
            record_id = int(cur.lastrowid)
            row = self._repo.conn.execute(
                "SELECT * FROM attendance WHERE id=?", (record_id,)).fetchone()
        return self._attendance_row(row)

    def latest_attendance(self, member: str) -> Optional[Dict[str, Any]]:
        with self._repo._lock:
            row = self._repo.conn.execute(
                """SELECT * FROM attendance WHERE member=?
                   ORDER BY happened_at DESC, id DESC LIMIT 1""",
                (member,)).fetchone()
        return self._attendance_row(row) if row else None

    def has_later_event(self, member: str, happened_at: str) -> bool:
        with self._repo._lock:
            row = self._repo.conn.execute(
                "SELECT 1 FROM attendance WHERE member=? AND happened_at>? LIMIT 1",
                (member, happened_at)).fetchone()
        return row is not None

    def list_attendance(self, member: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM attendance"
        params: tuple = ()
        if member:
            sql += " WHERE member=?"
            params = (member,)
        sql += " ORDER BY happened_at, id"
        with self._repo._lock:
            rows = self._repo.conn.execute(sql, params).fetchall()
        return [self._attendance_row(row) for row in rows]

    def insert_dispatch(self, member: str, item_id: Optional[int], status: str,
                        requested_at: str, evaluation: Dict[str, Any],
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        payload = json.dumps(evaluation, ensure_ascii=False, sort_keys=True)
        with self._repo._lock, self._repo.conn:
            cur = self._repo.conn.execute(
                """INSERT INTO dispatches(member, item_id, status, requested_at,
                   evaluation, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (member, item_id, status, requested_at, payload, actor, now, now),
            )
            dispatch_id = int(cur.lastrowid)
            row = self._repo.conn.execute(
                "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
        return self._dispatch_row(row)

    def update_dispatch(self, dispatch_id: int, status: str,
                        evaluation: Dict[str, Any]) -> Dict[str, Any]:
        now = utc_now()
        payload = json.dumps(evaluation, ensure_ascii=False, sort_keys=True)
        with self._repo._lock, self._repo.conn:
            self._repo.conn.execute(
                "UPDATE dispatches SET status=?, evaluation=?, updated_at=? WHERE id=?",
                (status, payload, now, dispatch_id),
            )
            row = self._repo.conn.execute(
                "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
        return self._dispatch_row(row)

    def list_dispatches(self, member: Optional[str] = None,
                        status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM dispatches"
        clauses, params = [], []
        if member:
            clauses.append("member=?")
            params.append(member)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._repo._lock:
            rows = self._repo.conn.execute(sql, tuple(params)).fetchall()
        return [self._dispatch_row(row) for row in rows]

    def pending_dispatches(self, member: str) -> List[Dict[str, Any]]:
        with self._repo._lock:
            rows = self._repo.conn.execute(
                "SELECT * FROM dispatches WHERE member=? AND status=? ORDER BY id",
                (member, STATUS_PENDING)).fetchall()
        return [self._dispatch_row(row) for row in rows]


class DispatchService:
    """派工门禁用例编排：登记、判定、留档、迟到补录后重新判定。"""

    def __init__(self, repository: Repository):
        self.repository = repository
        self.store = DispatchRepository(repository)

    def record_attendance(self, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, ATTENDANCE_ROLES)
        actor = require_text(actor, "actor", 100)
        member = require_text(payload.get("member"), "member", 100)
        kind = payload.get("kind")
        if kind not in ATTENDANCE_KINDS:
            raise ValidationError("kind必须是arrival或departure")
        if payload.get("happened_at") is None:
            moment = fatigue.utc_moment()
        else:
            moment = fatigue.ensure_not_future(
                fatigue.parse_moment(payload.get("happened_at")))
        if kind == "arrival":
            latest = self.store.latest_attendance(member)
            if latest and latest["kind"] == "arrival":
                # 同一队员重复到场沿用首次结果
                return {"record": latest, "reused": True, "reevaluated": []}
        happened_text = fatigue.format_moment(moment)
        backfill = self.store.has_later_event(member, happened_text)
        record = self.store.insert_attendance(member, kind, happened_text,
                                              backfill, actor)
        reevaluated = self._reevaluate_pending(member, actor)
        self.repository.append_audit("attendance", "attendance", record["id"],
                                     actor, {
                                         "member": member, "kind": kind,
                                         "happened_at": happened_text,
                                         "backfill": backfill,
                                         "reevaluated_dispatch_ids": [
                                             r["id"] for r in reevaluated],
                                     })
        return {"record": record, "reused": False, "reevaluated": reevaluated}

    def list_attendance(self, member: Optional[str], role: str) -> list:
        ensure_role(role, VIEW_ROLES)
        if member is not None:
            member = require_text(member, "member", 100)
        return self.store.list_attendance(member)

    def create_dispatch(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        member = require_text(payload.get("member"), "member", 100)
        item_id = payload.get("item_id")
        if item_id is not None:
            if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id < 1:
                raise ValidationError("item_id必须是正整数")
            self.repository.get_item(item_id)
        if payload.get("at") is not None:
            moment = fatigue.ensure_not_future(
                fatigue.parse_moment(payload.get("at"), "at"), "at")
        else:
            moment = fatigue.utc_moment()
        evaluation = self.evaluate_member(member, moment)
        status = STATUS_ASSIGNED if evaluation["eligible"] else STATUS_PENDING
        record = self.store.insert_dispatch(
            member, item_id, status, fatigue.format_moment(moment), evaluation, actor)
        self.repository.append_audit("dispatch", "dispatch", record["id"], actor, {
            "member": member, "item_id": item_id, "status": status,
            "requested_at": record["requested_at"],
            "work_hours": evaluation["work_hours"],
            "rest_hours": evaluation["rest_hours"],
            "exceeded_hours": {r["rule"]: r["exceeded_hours"]
                               for r in evaluation["reasons"]},
            "available_at": evaluation["available_at"],
        })
        return record

    def list_dispatches(self, role: str, member: Optional[str] = None,
                        status: Optional[str] = None) -> list:
        ensure_role(role, VIEW_ROLES)
        if member is not None:
            member = require_text(member, "member", 100)
        if status is not None and status not in DISPATCH_STATUSES:
            raise ValidationError("status必须是assigned或pending_rest")
        return self.store.list_dispatches(member, status)

    def fatigue_preview(self, member: str, at: Optional[str], role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        member = require_text(member, "member", 100)
        moment = fatigue.parse_moment(at, "at") if at else fatigue.utc_moment()
        return {"member": member, "at": fatigue.format_moment(moment),
                "evaluation": self.evaluate_member(member, moment)}

    def evaluate_member(self, member: str, moment=None) -> Dict[str, Any]:
        moment = moment or fatigue.utc_moment()
        events = [
            {"id": row["id"], "kind": row["kind"],
             "happened_at": fatigue.parse_moment(row["happened_at"])}
            for row in self.store.list_attendance(member)
        ]
        return fatigue.evaluate(events, moment)

    def _reevaluate_pending(self, member: str, actor: str) -> List[Dict[str, Any]]:
        """迟到补录等记录变更后，按当前现场记录重新判定该队员的待派记录。"""
        pending = self.store.pending_dispatches(member)
        if not pending:
            return []
        evaluation = self.evaluate_member(member, fatigue.utc_moment())
        changed = []
        for record in pending:
            if evaluation["eligible"]:
                updated = self.store.update_dispatch(
                    record["id"], STATUS_ASSIGNED, evaluation)
                self.repository.append_audit(
                    "dispatch_reevaluate", "dispatch", record["id"], actor, {
                        "member": member, "from": STATUS_PENDING,
                        "to": STATUS_ASSIGNED,
                        "work_hours": evaluation["work_hours"],
                        "rest_hours": evaluation["rest_hours"],
                    })
                changed.append(updated)
            elif evaluation != record["evaluation"]:
                changed.append(self.store.update_dispatch(
                    record["id"], STATUS_PENDING, evaluation))
        return changed
