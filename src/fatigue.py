"""疲劳判定：按最近一次离场计算连续作业与休息时长，给出派工资格。

夜班轮换门禁规则：
- 连续作业超过 MAX_CONTINUOUS_WORK_HOURS 小时，只进待休整；
- 距最近一次离场不足 MIN_REST_HOURS 小时，只进待休整；
- 休息满 MIN_REST_HOURS 小时后上一时段疲劳清零，可重新派工；
- 判定时说明超限时长（超出/不足的小时数）和可派工时。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .domain import ValidationError

MAX_CONTINUOUS_WORK_HOURS = 8.0
MIN_REST_HOURS = 2.0
_FUTURE_TOLERANCE = timedelta(minutes=5)


def parse_moment(value: Any, field: str = "happened_at") -> datetime:
    """解析ISO 8601时刻，缺省时区按UTC处理，统一返回UTC。"""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValidationError(f"{field}不能为空")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field}不是有效的ISO时间") from exc
    else:
        raise ValidationError(f"{field}必须是ISO时间字符串")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def format_moment(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def utc_moment() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def ensure_not_future(moment: datetime, field: str = "happened_at") -> datetime:
    if moment > utc_moment() + _FUTURE_TOLERANCE:
        raise ValidationError(f"{field}不能晚于当前时间")
    return moment


def evaluate(events: List[Dict[str, Any]], at: Any) -> Dict[str, Any]:
    """按最近一次离场计算疲劳，返回资格、超限时长和可派工时。

    events为队员的到场/离场记录：[{"kind": "arrival"|"departure",
    "happened_at": 时刻, "id": 可选}]。同一时段重复到场只认首次。
    """
    moment = parse_moment(at, "at")
    relevant = []
    for event in events:
        happened = parse_moment(event.get("happened_at"))
        if happened <= moment:
            relevant.append((happened, event.get("id") or 0, event.get("kind")))
    relevant.sort()
    arrivals = [h for h, _, kind in relevant if kind == "arrival"]
    departures = [h for h, _, kind in relevant if kind == "departure"]
    last_departure = departures[-1] if departures else None
    open_arrival = None
    for happened in arrivals:
        if last_departure is None or happened > last_departure:
            open_arrival = happened
            break
    on_site = open_arrival is not None
    if on_site:
        work_hours = (moment - open_arrival).total_seconds() / 3600.0
    elif last_departure is not None:
        starts = [h for h in arrivals if h <= last_departure]
        work_hours = (last_departure - starts[-1]).total_seconds() / 3600.0 if starts else 0.0
    else:
        work_hours = 0.0
    work_hours = max(0.0, work_hours)
    rest_hours = None if last_departure is None else (moment - last_departure).total_seconds() / 3600.0
    reasons = []
    if work_hours > MAX_CONTINUOUS_WORK_HOURS:
        actual = round(work_hours, 2)
        exceeded = round(work_hours - MAX_CONTINUOUS_WORK_HOURS, 2)
        reasons.append({
            "rule": "continuous_work",
            "limit_hours": MAX_CONTINUOUS_WORK_HOURS,
            "actual_hours": actual,
            "exceeded_hours": exceeded,
            "message": f"连续作业{actual:g}小时，超过{MAX_CONTINUOUS_WORK_HOURS:g}小时上限，超限{exceeded:g}小时",
        })
    if rest_hours is not None and rest_hours < MIN_REST_HOURS:
        actual = round(rest_hours, 2)
        exceeded = round(MIN_REST_HOURS - rest_hours, 2)
        reasons.append({
            "rule": "rest",
            "limit_hours": MIN_REST_HOURS,
            "actual_hours": actual,
            "exceeded_hours": exceeded,
            "message": f"休息{actual:g}小时，不足{MIN_REST_HOURS:g}小时，还差{exceeded:g}小时",
        })
    eligible = not reasons
    available_at = None
    note = None
    if not eligible:
        if last_departure is not None:
            available_at = format_moment(last_departure + timedelta(hours=MIN_REST_HOURS))
        else:
            note = "队员尚未离场，离场并休息满2小时后再派工"
    return {
        "eligible": eligible,
        "on_site": on_site,
        "work_hours": round(work_hours, 2),
        "rest_hours": None if rest_hours is None else round(rest_hours, 2),
        "reasons": reasons,
        "available_at": available_at,
        "note": note,
    }
