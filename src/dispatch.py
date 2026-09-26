from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .domain import (ConflictError, ValidationError, ensure_role, require_text)
from .repository import Repository
from .rules import (ATTENDANCE_ROLES, DISPATCH_ROLES, STATUS_ASSIGNED,
                    STATUS_HELD, VIEW_ROLES, fatigue_decision, iso, parse_time,
                    summarize_gate)

DISPATCH_ENTITY = '派工'
ATTENDANCE_ENTITY = '现场记录'


class DispatchService:
    """派工门禁：到场离场时刻、疲劳判定与派工留档。"""

    def __init__(self, repository: Repository, clock: Optional[Callable[[], Any]] = None):
        self.repository = repository
        self._clock = clock

    def _now(self):
        if self._clock is not None:
            return self._clock()
        from .audit import utc_now
        return parse_time(utc_now(), 'now')

    # 时刻与队员参数
    @staticmethod
    def _member(payload: Dict[str, Any]) -> tuple:
        code = require_text(payload.get('member_code'), 'member_code', 64)
        name = payload.get('member_name')
        if name is not None:
            name = require_text(name, 'member_name', 100)
        return code, name

    def _payload_time(self, payload: Dict[str, Any], field: str):
        value = payload.get(field)
        if value is None:
            return self._now()
        return parse_time(value, field)

    def _sessions(self, member_code: str) -> List[Dict[str, Any]]:
        return self.repository.list_attendance(member_code)

    def _gate(self, member_code: str, at) -> Dict[str, Any]:
        if self.repository.open_attendance(member_code) is not None:
            result = {"decision": "held", "on_duty": True, "continuous_hours": None,
                      "rest_hours": None, "overtime_hours": None,
                      "rest_short_hours": None, "available_hours": 0.0,
                      "eligible_at": None,
                      "reasons": ["队员尚未离场，不能重复派入任务区"]}
        else:
            result = fatigue_decision(self._sessions(member_code), at, now=at)
        result["message"] = summarize_gate(result)
        return result

    def _reevaluate(self, member_code: str) -> List[Dict[str, Any]]:
        """迟到补录改变资格时，该队员的待派记录按当前时刻重新判定。"""
        now = self._now()
        released = []
        for dispatch in self.repository.held_dispatches(member_code):
            gate = self._gate(member_code, now)
            if gate["decision"] == "eligible":
                self.repository.release_dispatch(dispatch["id"], gate)
                updated = self.repository.get_dispatch(dispatch["id"])
                self.repository.append_audit(
                    "dispatch_release", DISPATCH_ENTITY, updated["id"],
                    "system", {"member_code": member_code,
                               "dispatch_id": updated["id"],
                               "previous_status": STATUS_HELD,
                               "gate": gate})
                released.append(updated)
        return released

    # 到场 / 离场
    def check_in(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ATTENDANCE_ROLES)
        actor = require_text(actor, 'actor', 100)
        code, name = self._member(payload)
        arrival = self._payload_time(payload, 'arrival_at')
        # 同一队员重复到场：沿用首次结果（未离场的到场记录幂等返回）
        existing = self.repository.open_attendance(code)
        if existing is not None:
            existing["deduped"] = True
            existing["message"] = "队员尚未离场，沿用首次到场记录"
            return existing
        item_id = payload.get('item_id')
        if item_id is not None:
            if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id < 1:
                raise ValidationError('item_id必须是正整数')
            self.repository.get_item(item_id)
        lag = (self._now() - arrival).total_seconds()
        if lag > 60:
            raise ValidationError('到场时刻过早，迟到记录请使用补录接口')
        if lag < 0:
            raise ValidationError('到场时刻不能晚于当前时刻')
        record = self.repository.add_attendance(
            code, name, item_id, iso(arrival), None, 'realtime', actor)
        self.repository.append_audit(
            'attendance_arrival', ATTENDANCE_ENTITY, record['id'], actor,
            {'member_code': code, 'arrival_at': record['arrival_at'],
             'item_id': record['item_id']})
        return record

    def check_out(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ATTENDANCE_ROLES)
        actor = require_text(actor, 'actor', 100)
        code, _name = self._member(payload)
        depart = self._payload_time(payload, 'depart_at')
        existing = self.repository.open_attendance(code)
        if existing is None:
            raise ConflictError('该队员没有未离场的到场记录')
        arrival = parse_time(existing['arrival_at'], 'arrival_at')
        if depart < arrival:
            raise ValidationError('离场时刻不能早于到场时刻')
        if depart > self._now():
            raise ValidationError('离场时刻不能晚于当前时刻')
        record = self.repository.close_attendance(existing['id'], iso(depart))
        self.repository.append_audit(
            'attendance_departure', ATTENDANCE_ENTITY, record['id'], actor,
            {'member_code': code, 'arrival_at': record['arrival_at'],
             'depart_at': record['depart_at']})
        record['reevaluated'] = self._reevaluate(code)
        return record

    def backfill_attendance(self, payload: Dict[str, Any], actor: str,
                            role: str) -> Dict[str, Any]:
        """迟到补录：到场和离场时刻一起登记，随后重新判定待派记录。"""
        ensure_role(role, ATTENDANCE_ROLES)
        actor = require_text(actor, 'actor', 100)
        code, name = self._member(payload)
        arrival = parse_time(payload.get('arrival_at'), 'arrival_at')
        depart = parse_time(payload.get('depart_at'), 'depart_at')
        if depart <= arrival:
            raise ValidationError('离场时刻必须晚于到场时刻')
        if depart > self._now():
            raise ValidationError('补录时刻不能晚于当前时刻')
        # 到场已实时登记、离场漏记或错记：补录到场时刻与记录一致时补全/更正该记录
        open_record = self.repository.open_attendance(code)
        matched = open_record
        match_closed = None
        if matched is None:
            for session in self._sessions(code):
                if session.get('depart_at') and \
                        parse_time(session['arrival_at'], 'arrival_at') == arrival:
                    match_closed = session
                    matched = session
                    break
        elif parse_time(matched['arrival_at'], 'arrival_at') != arrival:
            raise ConflictError('该队员仍有未离场记录，且到场时刻不一致，请先登记离场')
        if matched is not None:
            for session in self._sessions(code):
                if session['id'] == matched['id']:
                    continue
                if not session.get('depart_at'):
                    continue
                old_arrive = parse_time(session['arrival_at'], 'arrival_at')
                old_depart = parse_time(session['depart_at'], 'depart_at')
                if arrival < old_depart and old_arrive < depart:
                    raise ConflictError('补录时段与其他现场记录重叠')
            record = self.repository.close_attendance(
                matched['id'], iso(depart), source='backfill', force=True)
            self.repository.append_audit(
                'attendance_backfill', ATTENDANCE_ENTITY, record['id'], actor,
                {'member_code': code, 'arrival_at': record['arrival_at'],
                 'depart_at': record['depart_at'],
                 'item_id': record['item_id'], 'corrected': bool(match_closed)})
            record['reevaluated'] = self._reevaluate(code)
            return record
        for session in self._sessions(code):
            if not session.get('depart_at'):
                continue
            old_arrive = parse_time(session['arrival_at'], 'arrival_at')
            old_depart = parse_time(session['depart_at'], 'depart_at')
            if arrival < old_depart and old_arrive < depart:
                raise ConflictError('补录时段与已有现场记录重叠')
        item_id = payload.get('item_id')
        if item_id is not None:
            if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id < 1:
                raise ValidationError('item_id必须是正整数')
            self.repository.get_item(item_id)
        record = self.repository.add_attendance(
            code, name, item_id, iso(arrival), iso(depart), 'backfill', actor)
        self.repository.append_audit(
            'attendance_backfill', ATTENDANCE_ENTITY, record['id'], actor,
            {'member_code': code, 'arrival_at': record['arrival_at'],
             'depart_at': record['depart_at'], 'item_id': record['item_id']})
        record['reevaluated'] = self._reevaluate(code)
        return record

    # 新派工
    def create_dispatch(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, 'actor', 100)
        code, name = self._member(payload)
        item_id = payload.get('item_id')
        if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id < 1:
            raise ValidationError('item_id必须是正整数')
        item = self.repository.get_item(item_id)
        if item['status'] == 'closed':
            raise ConflictError('任务区已关闭，不能派工')
        at = self._payload_time(payload, 'at')
        if at > self._now():
            raise ValidationError('派工时刻不能晚于当前时刻')
        gate = self._gate(code, at)
        status = STATUS_ASSIGNED if gate['decision'] == 'eligible' else STATUS_HELD
        dispatch = self.repository.add_dispatch(
            code, name, item_id, status, iso(at), gate, actor)
        self.repository.append_audit(
            'dispatch_create', DISPATCH_ENTITY, dispatch['id'], actor,
            {'member_code': code, 'item_id': item_id, 'status': status,
             'gate': gate})
        return dispatch

    # 查询（现场记录始终可查）
    def list_attendance(self, member_code: Optional[str], role: str) -> list:
        ensure_role(role, VIEW_ROLES)
        if member_code:
            member_code = require_text(member_code, 'member_code', 64)
        return self.repository.list_attendance(member_code)

    def list_dispatches(self, member_code: Optional[str],
                        status: Optional[str], role: str) -> list:
        ensure_role(role, VIEW_ROLES)
        if status is not None and status not in (STATUS_ASSIGNED, STATUS_HELD):
            raise ValidationError('status必须是assigned或held')
        return self.repository.list_dispatches(member_code, status)

    def member_status(self, member_code: str, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        member_code = require_text(member_code, 'member_code', 64)
        gate = self._gate(member_code, self._now())
        open_record = self.repository.open_attendance(member_code)
        return {'member_code': member_code,
                'open_attendance': open_record,
                'gate': gate}
