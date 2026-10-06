"""按角色的最小可见投影。

位置和病史是敏感信息：投影不是另存一份数据，而是从同一事件流
按角色过滤后生成只读视图。事件本身带有 role_required 标记。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from .aggregate import (
    HISTORY_VISIBLE_ROLES,
    LOCATION_VISIBLE_ROLES,
    EmergencyCase,
)
from .event import DomainEvent

REDACTED = "***按角色隐藏***"


@dataclass(frozen=True)
class RoleView:
    role: str
    case_id: str
    level_label: str | None
    locked: bool
    caller_number: str | None
    address: str | None  # 无权限时为 None
    location_authorized: bool
    symptom_quotes: tuple[str, ...]
    history: tuple[str, ...]  # 无权限时为空
    medications: tuple[str, ...]  # 用药随病史一同限制
    onset_time: datetime | None
    guidance: tuple[str, ...]
    redaction_notes: tuple[str, ...]


def project_case(case: EmergencyCase, role: str) -> RoleView:
    notes: list[str] = []
    address = case.address
    if address is not None and role not in LOCATION_VISIBLE_ROLES:
        address = None
        notes.append("位置信息对该角色不可见")
    # 家属撤回定位授权后，任何角色都不再看到精确位置
    if address is not None and not case.location_authorized:
        address = None
        notes.append("定位授权已撤回，位置不展示")

    if role in HISTORY_VISIBLE_ROLES:
        history = tuple(case.history)
        medications = tuple(case.medications)
    else:
        history, medications = (), ()
        if case.history or case.medications:
            notes.append("既往风险与用药对该角色不可见")

    return RoleView(
        role=role,
        case_id=case.case_id,
        level_label=case.level.label if case.level else None,
        locked=case.locked,
        caller_number=case.caller_number,
        address=address,
        location_authorized=case.location_authorized,
        symptom_quotes=tuple(case.symptom_quotes),
        history=history,
        medications=medications,
        onset_time=case.onset_authoritative,
        guidance=case.guidance,
        redaction_notes=tuple(notes),
    )


def project_events_for_role(events: Sequence[DomainEvent], role: str) -> tuple[dict[str, Any], ...]:
    """事件级投影：抹掉 role_required 不满足的敏感 payload 字段。"""
    out: list[dict[str, Any]] = []
    for event in events:
        data = event.to_dict()
        required = event.role_required
        if required and role not in LOCATION_VISIBLE_ROLES and required == "dispatcher":
            data["payload"] = {k: (REDACTED if k in {"address"} else v) for k, v in data["payload"].items()}
        out.append(data)
    return tuple(out)
