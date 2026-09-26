from __future__ import annotations
from datetime import datetime, timedelta, timezone
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

# 派工门禁：疲劳判定
MAX_WORK_HOURS=8.0          # 连续作业上限
MIN_REST_HOURS=2.0          # 最近一次离场后最少休息
ATTENDANCE_ROLES=set(['field_commander','logistics'])
DISPATCH_ROLES=set(['logistics'])
STATUS_ASSIGNED='assigned'
STATUS_HELD='held'

def parse_time(value,field):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}必须是ISO时间")
    text=value.strip()
    if text.endswith('Z'): text=text[:-1]+'+00:00'
    try: moment=datetime.fromisoformat(text)
    except ValueError as exc: raise ValidationError(f"{field}必须是ISO时间") from exc
    if moment.tzinfo is None: moment=moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)

def iso(moment): return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()

def hours_between(start,end): return max(0.0,(end-start).total_seconds()/3600.0)

def fatigue_decision(sessions,at,now=None):
    """按最近一次离场计算门禁结果。

    sessions为该队员已闭合的到场/离场记录列表，每项含arrival_at、depart_at。
    连续作业跨休息段累积：休息达到MIN_REST_HOURS则链条重置。
    返回decision（eligible/held）与超限时长、可派工时等明细。
    """
    now=now or at
    closed=sorted(
        [s for s in sessions if s.get('arrival_at') and s.get('depart_at')],
        key=lambda s:s['arrival_at'])
    if not closed:
        return {"decision":"eligible","on_duty":False,"continuous_hours":0.0,
                "rest_hours":None,"overtime_hours":0.0,"rest_short_hours":0.0,
                "available_hours":MAX_WORK_HOURS,"eligible_at":iso(now),"reasons":[]}
    last=closed[-1]
    last_depart=last['depart_at']
    if not isinstance(last_depart,datetime): last_depart=parse_time(last_depart,'depart_at')
    rest_hours=hours_between(last_depart,at)
    if rest_hours>=MIN_REST_HOURS:
        return {"decision":"eligible","on_duty":False,"continuous_hours":0.0,
                "rest_hours":round(rest_hours,2),"overtime_hours":0.0,"rest_short_hours":0.0,
                "available_hours":MAX_WORK_HOURS,"eligible_at":iso(now),"reasons":[]}
    # 休息不足：从最后一次完整休息后累积连续作业（含其间短休息）
    chain_end=last_depart
    chain_start=last['arrival_at']
    if not isinstance(chain_start,datetime): chain_start=parse_time(chain_start,'arrival_at')
    for prev in reversed(closed[:-1]):
        prev_depart=prev['depart_at']
        prev_arrive=prev['arrival_at']
        if not isinstance(prev_depart,datetime): prev_depart=parse_time(prev_depart,'depart_at')
        if not isinstance(prev_arrive,datetime): prev_arrive=parse_time(prev_arrive,'arrival_at')
        if hours_between(prev_depart,chain_start)>=MIN_REST_HOURS: break
        chain_start=prev_arrive
    continuous_hours=hours_between(chain_start,chain_end)
    overtime_hours=max(0.0,continuous_hours-MAX_WORK_HOURS)
    rest_short_hours=round(MIN_REST_HOURS-rest_hours,2)
    available_hours=round(max(0.0,MAX_WORK_HOURS-continuous_hours),2)
    reasons=[]
    if overtime_hours>0:
        reasons.append(f"连续作业{round(continuous_hours,2)}小时，超出8小时上限{round(overtime_hours,2)}小时")
    reasons.append(f"离场后仅休息{round(rest_hours,2)}小时，距2小时最低休息还差{rest_short_hours}小时")
    eligible_at=iso(last_depart+timedelta(hours=MIN_REST_HOURS))
    return {"decision":"held","on_duty":False,
            "continuous_hours":round(continuous_hours,2),
            "rest_hours":round(rest_hours,2),
            "overtime_hours":round(overtime_hours,2),
            "rest_short_hours":rest_short_hours,
            "available_hours":available_hours,
            "eligible_at":eligible_at,"reasons":reasons}

def summarize_gate(result):
    if result.get("on_duty"):
        return "仍在任务区，未离场，不能重复派工"
    if result["decision"]=="eligible":
        return f"可派工，可派工时{result['available_hours']}小时"
    parts=list(result["reasons"])
    parts.append(f"需休整至{result['eligible_at']}，当前可派工时{result['available_hours']}小时")
    return "；".join(parts)
