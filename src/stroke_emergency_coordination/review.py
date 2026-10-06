"""事后复盘：从不可变事件流还原每个判断、通知和交接时刻。

只做读取和重放，不产生新事实。时间线中每条记录都标注来源类型，
系统提示明确显示 ``source="system_hint"``，不会被还原成诊断结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .event import DomainEvent

# 复盘时间线关注的事件类别
JUDGMENT_EVENTS = {"TRIAGE_HINT_EMITTED", "PROFESSIONAL_DECISION_RECORDED", "RISK_LOCKED"}
NOTIFICATION_EVENTS = {"NOTIFICATION_SENT", "RECEIPT_ACKNOWLEDGED", "FAMILY_ACKNOWLEDGED"}
HANDOFF_EVENTS = {"HANDOFF_RECORDED"}
TIME_EVENTS = {"ONSET_TIME_CORRECTED"}


@dataclass(frozen=True)
class TimelineEntry:
    at: str
    category: str  # judgment / notification / handoff / time_correction / other
    source: str  # system_hint / professional / family / hospital / system
    event_type: str
    aggregate_id: str
    summary: str
    detail: dict[str, Any]

    def render_line(self) -> str:
        who = {
            "system_hint": "系统提示（非诊断）",
            "professional": "专业人员判断",
            "dispatcher": "调度接线员",
            "ambulance": "急救车/车队",
            "family": "家属",
            "hospital": "医院",
            "system": "系统",
        }.get(self.source, self.source)
        return f"[{self.at}] {self.event_type}｜{who}｜{self.summary}"


def _source_of(event: DomainEvent) -> str:
    kind = event.event_type
    operator = event.operator_id or ""
    if kind == "TRIAGE_HINT_EMITTED":
        return "system_hint"
    if kind == "PROFESSIONAL_DECISION_RECORDED":
        return "professional"
    if kind == "FAMILY_ACKNOWLEDGED":
        return "family"
    if kind in {"HOSPITAL_ACCEPTED", "HANDOFF_RECORDED"}:
        return "hospital"
    if operator.startswith("dispatcher"):
        return "dispatcher"
    if operator.startswith("fleet") or operator.startswith("A-"):
        return "ambulance"
    if operator.startswith("system"):
        return "system"
    return "professional"


def _category_of(event_type: str) -> str:
    if event_type in JUDGMENT_EVENTS:
        return "judgment"
    if event_type in NOTIFICATION_EVENTS:
        return "notification"
    if event_type in HANDOFF_EVENTS:
        return "handoff"
    if event_type in TIME_EVENTS:
        return "time_correction"
    return "other"


def build_timeline(events: Sequence[DomainEvent]) -> tuple[TimelineEntry, ...]:
    """跨聚合按发生时刻排序的完整复盘时间线。"""
    entries: list[TimelineEntry] = []
    for event in sorted(events, key=lambda e: e.occurred_at):  # 稳定排序：同一时刻保留提交时的因果顺序
        detail = dict(event.payload)
        if event.event_type == "TRIAGE_HINT_EMITTED":
            # 双保险：复盘视图再次钉死 is_diagnosis=False
            detail["is_diagnosis"] = False
        entries.append(
            TimelineEntry(
                at=event.occurred_at.isoformat(),
                category=_category_of(event.event_type),
                source=_source_of(event),
                event_type=event.event_type,
                aggregate_id=event.aggregate_id,
                summary=event.summary,
                detail=detail,
            )
        )
    return tuple(entries)


def render_timeline(events: Sequence[DomainEvent]) -> str:
    return "\n".join(entry.render_line() for entry in build_timeline(events))
