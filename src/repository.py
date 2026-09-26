from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_code TEXT NOT NULL,
                    member_name TEXT,
                    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
                    arrival_at TEXT NOT NULL,
                    depart_at TEXT,
                    source TEXT NOT NULL DEFAULT 'realtime'
                        CHECK(source IN ('realtime','backfill')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_attendance_member
                    ON attendance(member_code, arrival_at);
                CREATE TABLE IF NOT EXISTS dispatches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_code TEXT NOT NULL,
                    member_name TEXT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ('assigned','held')),
                    requested_at TEXT NOT NULL,
                    gate_detail TEXT NOT NULL,
                    reevaluated INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_dispatches_member
                    ON dispatches(member_code);
                CREATE INDEX IF NOT EXISTS ix_dispatches_status
                    ON dispatches(status);
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # 派工门禁：到场/离场与派工留档
    def add_attendance(self, member_code: str, member_name: Optional[str],
                       item_id: Optional[int], arrival_at: str,
                       depart_at: Optional[str], source: str,
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO attendance(member_code, member_name, item_id, arrival_at,
                   depart_at, source, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (member_code, member_name, item_id, arrival_at, depart_at,
                 source, actor, now),
            )
            attendance_id = int(cur.lastrowid)
        return self.get_attendance(attendance_id)

    def get_attendance(self, attendance_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM attendance WHERE id=?", (attendance_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("现场记录不存在")
        return dict(row)

    def open_attendance(self, member_code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM attendance WHERE member_code=? AND depart_at IS NULL
                   ORDER BY arrival_at DESC, id DESC LIMIT 1""",
                (member_code,),
            ).fetchone()
        return dict(row) if row else None

    def list_attendance(self, member_code: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM attendance"
        params: tuple = ()
        if member_code:
            sql += " WHERE member_code=?"
            params = (member_code,)
        sql += " ORDER BY arrival_at, id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def close_attendance(self, attendance_id: int, depart_at: str,
                         source: Optional[str] = None,
                         force: bool = False) -> Dict[str, Any]:
        with self._lock, self.conn:
            where = "id=?" if force else "id=? AND depart_at IS NULL"
            sets = ["depart_at=?"]
            params: list = [depart_at]
            if source is not None:
                sets.append("source=?")
                params.append(source)
            params.append(attendance_id)
            cur = self.conn.execute(
                f"UPDATE attendance SET {', '.join(sets)} WHERE {where}",
                tuple(params),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM attendance WHERE id=?", (attendance_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("现场记录不存在")
                raise ConflictError("该到场记录已离场")
        return self.get_attendance(attendance_id)

    def add_dispatch(self, member_code: str, member_name: Optional[str],
                     item_id: int, status: str, requested_at: str,
                     gate_detail: dict, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO dispatches(member_code, member_name, item_id, status,
                   requested_at, gate_detail, reevaluated, created_by, created_at,
                   updated_at) VALUES(?,?,?,?,?,? ,0,?,?,?)""",
                (member_code, member_name, item_id, status, requested_at,
                 json.dumps(gate_detail, ensure_ascii=False, sort_keys=True),
                 actor, now, now),
            )
            dispatch_id = int(cur.lastrowid)
        return self.get_dispatch(dispatch_id)

    def get_dispatch(self, dispatch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("派工记录不存在")
        result = dict(row)
        result["gate_detail"] = json.loads(result["gate_detail"])
        result["reevaluated"] = bool(result["reevaluated"])
        return result

    def list_dispatches(self, member_code: Optional[str] = None,
                        status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM dispatches"
        clauses = []
        params: list = []
        if member_code:
            clauses.append("member_code=?")
            params.append(member_code)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["gate_detail"] = json.loads(item["gate_detail"])
            item["reevaluated"] = bool(item["reevaluated"])
            result.append(item)
        return result

    def held_dispatches(self, member_code: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dispatches WHERE member_code=? AND status='held' ORDER BY id",
                (member_code,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["gate_detail"] = json.loads(item["gate_detail"])
            result.append(item)
        return result

    def release_dispatch(self, dispatch_id: int, gate_detail: dict) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE dispatches SET status='assigned', reevaluated=1,
                   gate_detail=?, updated_at=? WHERE id=? AND status='held'""",
                (json.dumps(gate_detail, ensure_ascii=False, sort_keys=True),
                 now, dispatch_id),
            )
