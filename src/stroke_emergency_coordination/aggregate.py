"""急症事件聚合 emergency_call。

不变量：
- 高危阈值首次达到即锁定（RISK_LOCKED），锁定不可撤销；
- 级别只升不降：后续资料只能增加上下文；
- 发病时间冲突的原始值全部保留，只有指定角色可以发布权威更正；
- 系统分诊提示与专业人员判断是两类不同事件，提示不得标记为诊断。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .errors import DomainError
from .event import DomainEvent, ensure_aware
from .triage import (
    GUIDANCE_DISCLAIMER,
    LOCKED_GUIDANCE,
    TriageLevel,
    evaluate_triage,
)

AGGREGATE_TYPE = "emergency_call"

# 有权限更正冲突发病时间的指定角色
DESIGNATED_CORRECTION_ROLES = frozenset({"supervisor", "senior_dispatcher"})

# 定位/病史的可见角色（供 access 投影使用）
LOCATION_VISIBLE_ROLES = frozenset({"dispatcher", "medical", "supervisor"})
HISTORY_VISIBLE_ROLES = frozenset({"medical", "supervisor"})

IdFactory = Callable[[str], str]


@dataclass
class OnsetReport:
    value: datetime
    source: str
    reported_at: datetime


@dataclass
class Acknowledgement:
    family_member: str
    at: datetime
    guidance_version: int
    channel: str


class EmergencyCase:
    def __init__(self, case_id: str, next_id: IdFactory) -> None:
        self.case_id = case_id
        self._next_id = next_id
        self._version = 0
        self.pending: list[DomainEvent] = []

        self.opened = False
        self.caller_number: str | None = None
        self.device_id: str | None = None
        self.symptom_quotes: list[str] = []
        self.history: list[str] = []
        self.medications: list[str] = []
        self.onset_reports: list[OnsetReport] = []
        self.onset_authoritative: datetime | None = None
        self.onset_conflict = False
        self.corrections: list[Mapping[str, Any]] = []
        self.location_authorized = False
        self.location_authorization_scope: str | None = None
        self.address: str | None = None
        self.level: TriageLevel | None = None
        self.locked = False
        self.locked_at: datetime | None = None
        self.guidance_version = 0
        self.guidance: tuple[str, ...] = ()
        self.acknowledgements: list[Acknowledgement] = []
        self.professional_decisions: list[Mapping[str, Any]] = []
        self.merged_from: list[str] = []
        self.closed = False

    # --- 回放 ---------------------------------------------------------
    @classmethod
    def replay(cls, case_id: str, events: Sequence[DomainEvent], next_id: IdFactory) -> "EmergencyCase":
        case = cls(case_id, next_id)
        for event in events:
            case.apply(event)
        case.pending.clear()
        return case

    def apply(self, event: DomainEvent) -> None:
        self._version = event.version
        p = event.payload
        kind = event.event_type
        if kind == "CALL_RECEIVED":
            self.opened = True
            self.caller_number = p.get("caller_number")
            self.device_id = p.get("device_id")
            self.address = p.get("address")
            self.location_authorized = bool(p.get("location_authorized", False))
            self.location_authorization_scope = p.get("location_scope")
        elif kind == "SYMPTOM_REPORTED":
            report_kind = p.get("kind", "symptom")
            text = p.get("text", "")
            if report_kind == "symptom":
                self.symptom_quotes.append(text)
            elif report_kind == "history":
                self.history.append(text)
            elif report_kind == "medication":
                self.medications.append(text)
            if p.get("onset_time"):
                report = OnsetReport(
                    value=_parse(p["onset_time"]),
                    source=p.get("source", "family"),
                    reported_at=event.occurred_at,
                )
                self._record_onset_report(report)
        elif kind == "LOCATION_AUTHORIZATION_GRANTED":
            self.location_authorized = True
            self.location_authorization_scope = p.get("scope", "precise")
            if p.get("address"):
                self.address = p["address"]
        elif kind == "LOCATION_AUTHORIZATION_REVOKED":
            self.location_authorized = False
        elif kind == "TRIAGE_HINT_EMITTED":
            level = TriageLevel(p["level"])
            self.level = level if self.level is None else TriageLevel(max(self.level, level))
        elif kind == "PROFESSIONAL_DECISION_RECORDED":
            self.professional_decisions.append(dict(p, decided_at=event.occurred_at.isoformat()))
            level = TriageLevel(p["level"])
            self.level = level if self.level is None else TriageLevel(max(self.level, level))
        elif kind == "RISK_LOCKED":
            self.locked = True
            self.locked_at = event.occurred_at
        elif kind == "ON_SCENE_GUIDANCE_ISSUED":
            self.guidance_version = int(p.get("guidance_version", self.guidance_version + 1))
            self.guidance = tuple(p.get("items", ()))
        elif kind == "FAMILY_ACKNOWLEDGED":
            self.acknowledgements.append(
                Acknowledgement(
                    family_member=p["family_member"],
                    at=event.occurred_at,
                    guidance_version=int(p["guidance_version"]),
                    channel=p.get("channel", "phone"),
                )
            )
        elif kind == "ONSET_TIME_CORRECTED":
            self.onset_authoritative = _parse(p["new_value"])
            self.onset_conflict = False
            self.corrections.append(dict(p, corrected_at=event.occurred_at.isoformat()))
        elif kind == "DUPLICATE_REPORT_MERGED":
            self.merged_from.append(p["other_case_id"])

    def _record_onset_report(self, report: OnsetReport) -> None:
        if not self.onset_reports:
            self.onset_authoritative = report.value
        else:
            known = {r.value for r in self.onset_reports}
            if self.onset_authoritative is not None:
                known.add(self.onset_authoritative)
            if report.value not in known:
                # 原始冲突值保留在 onset_reports；权威值不自动改写，等待指定人员更正
                self.onset_conflict = True
        self.onset_reports.append(report)

    # --- 发事件 -------------------------------------------------------
    def _emit(
        self,
        event_type: str,
        summary: str,
        payload: Mapping[str, Any],
        at: datetime,
        *,
        operator_id: str | None = None,
        role_required: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> DomainEvent:
        ensure_aware(at)
        event = DomainEvent(
            event_id=self._next_id(event_type.lower()),
            event_type=event_type,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=self.case_id,
            occurred_at=at,
            version=self._version + 1,
            summary=summary,
            payload=dict(payload),
            operator_id=operator_id,
            role_required=role_required,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        self.pending.append(event)
        self.apply(event)
        return event

    # --- 命令 ---------------------------------------------------------
    def open(
        self,
        *,
        caller_number: str,
        device_id: str | None,
        operator_id: str,
        at: datetime,
        address: str | None = None,
        location_authorized: bool = False,
        location_scope: str = "precise",
    ) -> DomainEvent:
        if self.opened:
            raise DomainError("case_already_open", f"事件 {self.case_id} 已登记")
        return self._emit(
            "CALL_RECEIVED",
            f"接入 {caller_number} 的急救来电",
            {
                "caller_number": caller_number,
                "device_id": device_id,
                "address": address,
                "location_authorized": location_authorized,
                "location_scope": location_scope if location_authorized else None,
            },
            at,
            operator_id=operator_id,
            role_required="dispatcher" if address else None,
        )

    def report(
        self,
        *,
        kind: str,
        text: str,
        source: str,
        operator_id: str,
        at: datetime,
        onset_time: datetime | None = None,
    ) -> tuple[DomainEvent, ...]:
        if not self.opened:
            raise DomainError("case_not_open", "必须先登记来电")
        if kind not in {"symptom", "history", "medication"}:
            raise DomainError("unsupported_report_kind", f"未知登记类型：{kind}")
        produced: list[DomainEvent] = []
        payload: dict[str, Any] = {"kind": kind, "text": text, "source": source}
        if onset_time is not None:
            ensure_aware(onset_time)
            payload["onset_time"] = onset_time.isoformat()
        produced.append(
            self._emit(
                "SYMPTOM_REPORTED",
                f"登记{ {'symptom': '症状原话', 'history': '既往风险', 'medication': '用药'}[kind] }",
                payload,
                at,
                operator_id=operator_id,
            )
        )
        # 任何补充资料后重跑分诊：只增上下文，级别取历史最大值
        produced.extend(self._run_triage(at, operator_id))
        return tuple(produced)

    def _run_triage(self, at: datetime, operator_id: str) -> list[DomainEvent]:
        result = evaluate_triage(self.symptom_quotes, self.history, self.medications)
        monotonic_level = (
            result.level if self.level is None else TriageLevel(max(self.level, result.level))
        )
        hint_text = result.hint_text
        if monotonic_level != result.level:
            hint_text += (
                f"本次登记内容本身评估为{result.level.label}，"
                f"但事件历史级别为{monotonic_level.label}，级别只升不降，维持{monotonic_level.label}。"
            )
        hint = self._emit(
            "TRIAGE_HINT_EMITTED",
            f"自动分诊提示：{monotonic_level.label}级别（非诊断）",
            {
                "level": monotonic_level.value,
                "level_label": monotonic_level.label,
                "raw_level": result.level.value,
                "is_diagnosis": False,
                "hint_text": hint_text,
                "matched_rules": [
                    {
                        "code": rule.code,
                        "level": rule.level.value,
                        "description": rule.description,
                        "evidence": rule.evidence,
                    }
                    for rule in result.matched
                ],
            },
            at,
            operator_id="system-triage",
            causation_id=self.pending[-1].event_id if self.pending else None,
        )
        produced = [hint]
        if monotonic_level >= TriageLevel.RED and not self.locked:
            produced.extend(
                self._lock(at, "system-triage", trigger_event_id=hint.event_id, trigger="自动分诊阈值")
            )
        return produced

    def _lock(self, at: datetime, operator_id: str, *, trigger_event_id: str, trigger: str) -> list[DomainEvent]:
        locked = self._emit(
            "RISK_LOCKED",
            "达到高危阈值，事件锁定并启动转运选择",
            {
                "level": TriageLevel.RED.value,
                "trigger": trigger,
                "monotonic": True,
                "note": "锁定后补充资料只能增加上下文，不能降低级别",
            },
            at,
            operator_id=operator_id,
            causation_id=trigger_event_id,
        )
        guidance = self._emit(
            "ON_SCENE_GUIDANCE_ISSUED",
            "向现场发出等待期间照护与禁忌指导",
            {
                "guidance_version": 1,
                "items": list(LOCKED_GUIDANCE),
                "disclaimer": GUIDANCE_DISCLAIMER,
                "is_diagnosis": False,
            },
            at,
            operator_id="system-triage",
            causation_id=locked.event_id,
        )
        return [locked, guidance]

    def record_professional_decision(
        self,
        *,
        professional_id: str,
        role: str,
        level: TriageLevel,
        assessment: str,
        at: datetime,
    ) -> tuple[DomainEvent, ...]:
        """记录专业人员判断。它是独立的判断来源，可以升级，但不能为系统提示背书为诊断。"""
        if not self.opened:
            raise DomainError("case_not_open", "必须先登记来电")
        decision = self._emit(
            "PROFESSIONAL_DECISION_RECORDED",
            f"专业人员判断：{level.label}级别",
            {
                "professional_id": professional_id,
                "role": role,
                "level": level.value,
                "level_label": level.label,
                "assessment": assessment,
                "is_professional_judgment": True,
            },
            at,
            operator_id=professional_id,
        )
        produced = [decision]
        if level >= TriageLevel.RED and not self.locked:
            produced.extend(
                self._lock(at, professional_id, trigger_event_id=decision.event_id, trigger="专业人员判断")
            )
        return tuple(produced)

    def grant_location(self, *, operator_id: str, scope: str, address: str | None, at: datetime) -> DomainEvent:
        if not self.opened:
            raise DomainError("case_not_open", "必须先登记来电")
        return self._emit(
            "LOCATION_AUTHORIZATION_GRANTED",
            "获得家属/来电设备的定位授权",
            {"scope": scope, "address": address},
            at,
            operator_id=operator_id,
            role_required="dispatcher",
        )

    def revoke_location(self, *, operator_id: str, at: datetime) -> DomainEvent:
        return self._emit(
            "LOCATION_AUTHORIZATION_REVOKED",
            "定位授权撤回，后续投影不再展示位置",
            {},
            at,
            operator_id=operator_id,
        )

    def acknowledge_guidance(
        self, *, family_member: str, at: datetime, channel: str = "phone"
    ) -> DomainEvent:
        if not self.locked or self.guidance_version == 0:
            raise DomainError("guidance_not_issued", "尚无已锁定的现场指导可供确认")
        already = {a.guidance_version for a in self.acknowledgements}
        if self.guidance_version in already:
            raise DomainError("already_acknowledged", "该版本指导已被家属确认")
        return self._emit(
            "FAMILY_ACKNOWLEDGED",
            f"家属{family_member}确认收到现场指导",
            {
                "family_member": family_member,
                "guidance_version": self.guidance_version,
                "channel": channel,
            },
            at,
            operator_id=family_member,
        )

    def correct_onset_time(
        self,
        *,
        operator_id: str,
        operator_role: str,
        new_value: datetime,
        reason: str,
        at: datetime,
    ) -> DomainEvent:
        if operator_role not in DESIGNATED_CORRECTION_ROLES:
            raise DomainError(
                "forbidden_time_correction",
                f"角色 {operator_role} 无权更正发病时间，需指定人员",
            )
        ensure_aware(new_value)
        previous = self.onset_authoritative
        return self._emit(
            "ONSET_TIME_CORRECTED",
            "指定人员更正发病时间，原值保留可追溯",
            {
                "previous_value": previous.isoformat() if previous else None,
                "new_value": new_value.isoformat(),
                "reason": reason,
                "original_reports": [
                    {"value": r.value.isoformat(), "source": r.source, "reported_at": r.reported_at.isoformat()}
                    for r in self.onset_reports
                ],
            },
            at,
            operator_id=operator_id,
        )

    def record_merge(self, *, other_case_id: str, at: datetime) -> DomainEvent:
        if other_case_id in self.merged_from:
            raise DomainError("already_merged", f"{other_case_id} 已合并")
        return self._emit(
            "DUPLICATE_REPORT_MERGED",
            f"合并相同来电/设备的重复上报 {other_case_id}",
            {"other_case_id": other_case_id},
            at,
            operator_id="system-dedup",
        )

    def record_duplicate_rejected(self, *, other_report_id: str, reason: str, at: datetime) -> DomainEvent:
        return self._emit(
            "DUPLICATE_REPORT_REJECTED",
            f"重复上报 {other_report_id} 无新增信息，登记后忽略",
            {"other_report_id": other_report_id, "reason": reason},
            at,
            operator_id="system-dedup",
        )

    def record_notification(
        self,
        *,
        channel: str,
        target: str,
        target_role: str,
        at: datetime,
        ref_event_id: str,
        summary_text: str,
    ) -> DomainEvent:
        """对外通知（医院、车队、家属）落同一时间线，便于复盘通知时刻。"""
        return self._emit(
            "NOTIFICATION_SENT",
            summary_text,
            {
                "channel": channel,
                "target": target,
                "target_role": target_role,
                "ref_event_id": ref_event_id,
                "receipt_status": "pending",
            },
            at,
            operator_id="system-notify",
            causation_id=ref_event_id,
        )

    def acknowledge_receipt(self, *, notification_event_id: str, at: datetime, actor: str) -> DomainEvent:
        return self._emit(
            "RECEIPT_ACKNOWLEDGED",
            f"通知 {notification_event_id} 收到确认回执",
            {"notification_event_id": notification_event_id, "actor": actor},
            at,
            operator_id=actor,
            causation_id=notification_event_id,
        )

    def close(self, *, operator_id: str, at: datetime, reason: str) -> DomainEvent:
        return self._emit(
            "CASE_CLOSED",
            "事件闭环",
            {"reason": reason},
            at,
            operator_id=operator_id,
        )

    @property
    def version(self) -> int:
        return self._version


def _parse(value: str) -> datetime:
    from .event import parse_time

    return parse_time(value)
