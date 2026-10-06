"""急症预警转运协同服务。

在 contracts.py 的交换契约之上实现业务流程：

- 登记来电并合并同一来电人或设备的重复上报；
- 记录症状原话、发病时间、既往风险、用药、定位授权与家属确认；
- 系统自动提示与专业人员判断分开记录，系统提示不作为诊断结论；
- 高危信号达到阈值即锁定事件，下发禁忌指导并启动转运选择；
- 补充资料只增加上下文，不降低已经触发的级别；
- 发病时间冲突由指定负责人更正并保留原值；
- 医院临时满负荷只重算未出发的转运路线；
- 位置与病史按角色最小可见；
- 急救车失联时本地状态继续推进，恢复后只补传未确认回执；
- 复盘可还原每个判断、通知与交接时刻。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .contracts import validate_event
from .models import (
    SYSTEM_HINT_DISCLAIMER,
    STANDARD_GUIDANCE,
    LEVEL_RANK,
    ROLE_VISIBLE_FIELDS,
    AmbulanceState,
    AmbulanceStatus,
    Assessment,
    AssessmentSource,
    Actor,
    AuditEntry,
    EmergencyEvent,
    EventStatus,
    FamilyConfirmation,
    GuidanceInstruction,
    HospitalCapacity,
    Medication,
    OnsetCorrection,
    Receipt,
    Role,
    Supplement,
    SymptomEntry,
    TransportPhase,
    TransportTask,
    TriageLevel,
    TriageRule,
    higher_level,
)


class CoordinationError(Exception):
    """协同服务的基础异常。"""


class NotFoundError(CoordinationError):
    """聚合或任务不存在。"""


class PermissionDeniedError(CoordinationError):
    """角色无权执行该操作。"""


class ConflictError(CoordinationError):
    """业务状态冲突，例如发病时间已被登记。"""


class ContractViolationError(CoordinationError):
    """待发出的领域事件未通过交换契约校验。"""


@dataclass(frozen=True)
class RegistrationResult:
    event_id: str
    merged: bool


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CoordinationService:
    def __init__(
        self,
        schema: Mapping[str, Any],
        *,
        merge_window_minutes: int = 30,
        lock_level: TriageLevel = TriageLevel.CRITICAL,
    ) -> None:
        self._schema = schema
        self._merge_window_minutes = merge_window_minutes
        self._lock_level = lock_level
        self._events: dict[str, EmergencyEvent] = {}
        self._tasks: dict[str, TransportTask] = {}
        self._hospitals: dict[str, HospitalCapacity] = {}
        self._ambulances: dict[str, AmbulanceState] = {}
        self._triage_rules: list[TriageRule] = []
        self._exchange_events: list[dict[str, Any]] = []
        self._versions: dict[str, int] = {}
        self._counters = {"event": 0, "task": 0, "receipt": 0}

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _next_id(self, kind: str, prefix: str) -> str:
        self._counters[kind] += 1
        return f"{prefix}-{self._counters[kind]:04d}"

    def get_event(self, event_id: str) -> EmergencyEvent:
        event = self._events.get(event_id)
        if event is None:
            raise NotFoundError(f"事件不存在: {event_id}")
        return event

    def get_task(self, task_id: str) -> TransportTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"转运任务不存在: {task_id}")
        return task

    @property
    def exchange_events(self) -> list[dict[str, Any]]:
        """已通过契约校验、可对外交换的领域事件。"""
        return list(self._exchange_events)

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        summary: str,
    ) -> dict[str, Any]:
        version = self._versions.get(aggregate_id, 0) + 1
        payload = {
            "event_id": f"{aggregate_id}-{event_type.lower()}-{version}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at,
            "version": version,
            "summary": summary,
        }
        issues = validate_event(payload, self._schema)
        if issues:
            raise ContractViolationError(
                "; ".join(f"{issue.field}:{issue.code}" for issue in issues)
            )
        self._versions[aggregate_id] = version
        self._exchange_events.append(payload)
        return payload

    def _log(
        self,
        event: EmergencyEvent,
        kind: str,
        actor: Actor,
        occurred_at: str,
        **detail: Any,
    ) -> None:
        event.audit.append(
            AuditEntry(
                seq=len(event.audit) + 1,
                occurred_at=occurred_at,
                kind=kind,
                actor_id=actor.actor_id,
                detail=detail,
            )
        )
        event.last_activity_at = occurred_at

    def _notify(
        self,
        event: EmergencyEvent,
        occurred_at: str,
        channel: str,
        content: str,
    ) -> None:
        self._log(
            event,
            "notification",
            Actor("system", Role.SYSTEM),
            occurred_at,
            channel=channel,
            content=content,
        )

    # ------------------------------------------------------------------
    # 登记与合并
    # ------------------------------------------------------------------

    def register_call(
        self,
        *,
        caller_id: str,
        device_id: str | None,
        occurred_at: str,
        summary: str,
        actor: Actor,
    ) -> RegistrationResult:
        """登记来电；同一来电人或设备在时间窗内的重复上报合并到已有事件。"""
        existing = self._find_duplicate(caller_id, device_id, occurred_at)
        if existing is not None:
            self._log(
                existing,
                "duplicate_merged",
                actor,
                occurred_at,
                caller_id=caller_id,
                device_id=device_id,
                note="重复上报已合并，仅补充上下文",
            )
            return RegistrationResult(event_id=existing.event_id, merged=True)

        event_id = self._next_id("event", "EC")
        event = EmergencyEvent(
            event_id=event_id,
            caller_id=caller_id,
            device_id=device_id,
            status=EventStatus.OPEN,
            created_at=occurred_at,
            last_activity_at=occurred_at,
        )
        self._events[event_id] = event
        self._log(event, "call_registered", actor, occurred_at, summary=summary)
        self._emit("CALL_RECEIVED", "emergency_call", event_id, occurred_at, summary)
        return RegistrationResult(event_id=event_id, merged=False)

    def _find_duplicate(
        self, caller_id: str, device_id: str | None, occurred_at: str
    ) -> EmergencyEvent | None:
        moment = _parse_ts(occurred_at)
        window_seconds = self._merge_window_minutes * 60
        for event in self._events.values():
            if event.status == EventStatus.CLOSED:
                continue
            same_source = event.caller_id == caller_id or (
                device_id is not None and event.device_id == device_id
            )
            if not same_source:
                continue
            delta = abs((moment - _parse_ts(event.last_activity_at)).total_seconds())
            if delta <= window_seconds:
                return event
        return None

    # ------------------------------------------------------------------
    # 病情资料登记
    # ------------------------------------------------------------------

    def record_symptom(
        self,
        event_id: str,
        *,
        verbatim: str,
        occurred_at: str,
        actor: Actor,
    ) -> None:
        """记录症状原话，不改写家属表述。"""
        event = self.get_event(event_id)
        event.symptoms.append(
            SymptomEntry(verbatim=verbatim, recorded_at=occurred_at, recorded_by=actor.actor_id)
        )
        self._log(event, "symptom_recorded", actor, occurred_at, verbatim=verbatim)

    def record_onset(
        self,
        event_id: str,
        *,
        onset_at: str,
        occurred_at: str,
        actor: Actor,
    ) -> None:
        """首次登记发病时间；与已登记值冲突时须由指定负责人更正。"""
        event = self.get_event(event_id)
        if event.onset_at is not None and event.onset_at != onset_at:
            self._log(
                event,
                "onset_conflict",
                actor,
                occurred_at,
                existing=event.onset_at,
                reported=onset_at,
            )
            raise ConflictError("发病时间存在冲突，需指定负责人更正")
        event.onset_at = onset_at
        self._log(event, "onset_recorded", actor, occurred_at, onset_at=onset_at)

    def correct_onset_time(
        self,
        event_id: str,
        *,
        new_onset_at: str,
        reason: str,
        occurred_at: str,
        actor: Actor,
    ) -> None:
        """仅指定负责人可更正发病时间，原值保留在更正记录中。"""
        if actor.role != Role.SUPERVISOR:
            raise PermissionDeniedError("发病时间只能由指定负责人更正")
        event = self.get_event(event_id)
        event.onset_corrections.append(
            OnsetCorrection(
                original=event.onset_at,
                corrected=new_onset_at,
                corrected_by=actor.actor_id,
                occurred_at=occurred_at,
                reason=reason,
            )
        )
        event.onset_at = new_onset_at
        self._log(
            event,
            "onset_corrected",
            actor,
            occurred_at,
            corrected=new_onset_at,
            reason=reason,
        )

    def record_risk_history(
        self,
        event_id: str,
        *,
        items: list[str],
        occurred_at: str,
        actor: Actor,
    ) -> None:
        event = self.get_event(event_id)
        event.risk_history.extend(items)
        self._log(event, "risk_history_recorded", actor, occurred_at, items=list(items))

    def record_medication(
        self,
        event_id: str,
        *,
        name: str,
        dose: str,
        note: str,
        occurred_at: str,
        actor: Actor,
    ) -> None:
        event = self.get_event(event_id)
        event.medications.append(
            Medication(name=name, dose=dose, note=note, recorded_at=occurred_at)
        )
        self._log(event, "medication_recorded", actor, occurred_at, name=name, dose=dose)

    def authorize_location(
        self,
        event_id: str,
        *,
        granted: bool,
        location: str | None = None,
        occurred_at: str,
        actor: Actor,
    ) -> None:
        """登记定位授权；未授权时不保存位置。"""
        event = self.get_event(event_id)
        event.location_consent = granted
        event.location = location if granted else None
        self._log(event, "location_authorization", actor, occurred_at, granted=granted)

    def confirm_by_family(
        self,
        event_id: str,
        *,
        confirmer: str,
        content: str,
        occurred_at: str,
        actor: Actor,
    ) -> None:
        event = self.get_event(event_id)
        event.family_confirmations.append(
            FamilyConfirmation(confirmer=confirmer, content=content, occurred_at=occurred_at)
        )
        self._log(
            event, "family_confirmation", actor, occurred_at, confirmer=confirmer, content=content
        )

    # ------------------------------------------------------------------
    # 分诊规则与评估
    # ------------------------------------------------------------------

    def register_triage_rule(
        self,
        *,
        rule_id: str,
        description: str,
        keywords: list[str],
        threshold: int,
    ) -> None:
        if threshold < 1:
            raise CoordinationError("分诊规则阈值必须为正整数")
        self._triage_rules.append(
            TriageRule(
                rule_id=rule_id,
                description=description,
                keywords=tuple(keywords),
                threshold=threshold,
            )
        )

    def evaluate_rules(
        self,
        event_id: str,
        *,
        occurred_at: str,
    ) -> Assessment | None:
        """按已登记规则产生系统自动提示；提示不是诊断结论。"""
        event = self.get_event(event_id)
        text = " ".join(entry.verbatim for entry in event.symptoms)
        hits: list[str] = []
        for rule in self._triage_rules:
            matched = [keyword for keyword in rule.keywords if keyword in text]
            if len(matched) >= rule.threshold:
                hits.extend(matched)
        if not hits:
            return None
        return self.assess(
            event_id,
            source=AssessmentSource.SYSTEM_HINT,
            level=TriageLevel.CRITICAL,
            signals=sorted(set(hits)),
            actor=Actor("system", Role.SYSTEM),
            occurred_at=occurred_at,
            note=SYSTEM_HINT_DISCLAIMER,
        )

    def assess(
        self,
        event_id: str,
        *,
        source: AssessmentSource,
        level: TriageLevel,
        signals: list[str],
        actor: Actor,
        occurred_at: str,
        note: str = "",
    ) -> Assessment:
        """登记评估。系统提示与专业人员判断分开保存，互不改写。"""
        event = self.get_event(event_id)
        if source == AssessmentSource.PROFESSIONAL and actor.role not in (
            Role.MEDIC,
            Role.SUPERVISOR,
        ):
            raise PermissionDeniedError("专业判断只能由急救人员或调度负责人作出")
        if source == AssessmentSource.SYSTEM_HINT and actor.role != Role.SYSTEM:
            raise PermissionDeniedError("系统提示只能由规则引擎产生")

        assessment = Assessment(
            source=source,
            level=level,
            signals=tuple(signals),
            actor_id=actor.actor_id,
            occurred_at=occurred_at,
            note=note,
        )
        event.assessments.append(assessment)

        channel = (
            "system_level" if source == AssessmentSource.SYSTEM_HINT else "professional_level"
        )
        previous = getattr(event, channel)
        merged = higher_level(previous, level)
        setattr(event, channel, merged)
        detail: dict[str, Any] = {
            "source": source.value,
            "level": level.value,
            "signals": list(signals),
            "note": note,
        }
        if source == AssessmentSource.SYSTEM_HINT:
            detail["nature"] = SYSTEM_HINT_DISCLAIMER
        if previous is not None and LEVEL_RANK[level] < LEVEL_RANK[previous]:
            detail["level_floor_kept"] = previous.value
        self._log(event, "assessment", actor, occurred_at, **detail)

        self._maybe_lock(event, occurred_at)
        return assessment

    def add_supplement(
        self,
        event_id: str,
        *,
        note: str,
        occurred_at: str,
        actor: Actor,
        suggested_level: TriageLevel | None = None,
    ) -> None:
        """补充资料只增加上下文；已经触发的级别不因此降低。"""
        event = self.get_event(event_id)
        event.supplements.append(
            Supplement(
                note=note,
                actor_id=actor.actor_id,
                occurred_at=occurred_at,
                suggested_level=suggested_level,
            )
        )
        detail: dict[str, Any] = {"note": note}
        if suggested_level is not None:
            detail["suggested_level"] = suggested_level.value
            if LEVEL_RANK[suggested_level] > LEVEL_RANK[event.effective_level]:
                event.context_level = higher_level(event.context_level, suggested_level)
            else:
                detail["level_floor_kept"] = event.effective_level.value
        self._log(event, "supplement_added", actor, occurred_at, **detail)
        self._maybe_lock(event, occurred_at)

    def _maybe_lock(self, event: EmergencyEvent, occurred_at: str) -> None:
        if event.status != EventStatus.OPEN:
            return
        if LEVEL_RANK[event.effective_level] < LEVEL_RANK[self._lock_level]:
            return
        event.status = EventStatus.LOCKED
        event.locked_level = event.effective_level
        event.locked_at = occurred_at
        self._log(
            event,
            "risk_locked",
            Actor("system", Role.SYSTEM),
            occurred_at,
            level=event.locked_level.value,
        )
        for text in STANDARD_GUIDANCE:
            event.guidance.append(GuidanceInstruction(text=text, issued_at=occurred_at))
        self._log(
            event,
            "guidance_issued",
            Actor("system", Role.SYSTEM),
            occurred_at,
            instructions=list(STANDARD_GUIDANCE),
        )
        self._notify(
            event,
            occurred_at,
            "family",
            "；".join(STANDARD_GUIDANCE),
        )
        task_id = self._next_id("task", "TT")
        candidates = [
            hospital_id
            for hospital_id, capacity in self._hospitals.items()
            if capacity.accepting
        ]
        self._tasks[task_id] = TransportTask(
            task_id=task_id,
            event_id=event.event_id,
            phase=TransportPhase.SELECTING,
            candidate_hospitals=candidates,
        )
        event.transport_task_id = task_id
        self._log(
            event,
            "transport_selection_started",
            Actor("system", Role.SYSTEM),
            occurred_at,
            task_id=task_id,
            candidate_hospitals=list(candidates),
        )
        self._emit(
            "RISK_LOCKED",
            "emergency_call",
            event.event_id,
            occurred_at,
            f"高危信号达到阈值，事件 {event.event_id} 已锁定并启动转运选择",
        )

    # ------------------------------------------------------------------
    # 转运协同
    # ------------------------------------------------------------------

    def register_hospital(
        self,
        hospital_id: str,
        *,
        name: str,
        accepting: bool = True,
        occurred_at: str,
    ) -> None:
        self._hospitals[hospital_id] = HospitalCapacity(
            hospital_id=hospital_id, name=name, accepting=accepting, updated_at=occurred_at
        )

    def register_ambulance(self, ambulance_id: str, *, occurred_at: str) -> None:
        self._ambulances[ambulance_id] = AmbulanceState(
            ambulance_id=ambulance_id,
            status=AmbulanceStatus.AVAILABLE,
            updated_at=occurred_at,
        )

    def update_ambulance_status(
        self,
        ambulance_id: str,
        *,
        status: AmbulanceStatus,
        occurred_at: str,
    ) -> None:
        ambulance = self._ambulances.get(ambulance_id)
        if ambulance is None:
            raise NotFoundError(f"急救车不存在: {ambulance_id}")
        ambulance.status = status
        ambulance.updated_at = occurred_at

    def assign_transport(
        self,
        event_id: str,
        *,
        ambulance_id: str,
        hospital_id: str,
        occurred_at: str,
        actor: Actor,
    ) -> TransportTask:
        event = self.get_event(event_id)
        if event.transport_task_id is None:
            raise ConflictError("事件尚未锁定，未启动转运选择")
        task = self.get_task(event.transport_task_id)
        capacity = self._hospitals.get(hospital_id)
        if capacity is not None and not capacity.accepting:
            raise ConflictError(f"医院 {hospital_id} 当前满负荷，无法指派")
        task.ambulance_id = ambulance_id
        task.hospital_id = hospital_id
        task.phase = TransportPhase.ASSIGNED
        if ambulance_id in self._ambulances:
            self._ambulances[ambulance_id].status = AmbulanceStatus.DISPATCHED
            self._ambulances[ambulance_id].updated_at = occurred_at
        self._log(
            event,
            "transport_assigned",
            actor,
            occurred_at,
            task_id=task.task_id,
            ambulance_id=ambulance_id,
            hospital_id=hospital_id,
        )
        self._notify(event, occurred_at, "ambulance", f"请前往事件 {event_id} 现场")
        self._notify(event, occurred_at, "hospital", f"医院 {hospital_id} 准备接诊 {event_id}")
        self._emit(
            "TRANSPORT_ASSIGNED",
            "transport_task",
            task.task_id,
            occurred_at,
            f"事件 {event_id} 已指派急救车 {ambulance_id} 前往医院 {hospital_id}",
        )
        return task

    def hospital_accept(
        self,
        task_id: str,
        *,
        occurred_at: str,
        actor: Actor,
    ) -> None:
        task = self.get_task(task_id)
        event = self.get_event(task.event_id)
        self._log(event, "hospital_accepted", actor, occurred_at, task_id=task_id)
        self._emit(
            "HOSPITAL_ACCEPTED",
            "transport_task",
            task_id,
            occurred_at,
            f"医院已确认接诊事件 {task.event_id}",
        )

    def depart(
        self,
        task_id: str,
        *,
        occurred_at: str,
        actor: Actor,
    ) -> None:
        task = self.get_task(task_id)
        if task.phase != TransportPhase.ASSIGNED:
            raise ConflictError("只有已指派且未出发的任务可以出发")
        task.phase = TransportPhase.DEPARTED
        self._log(
            self.get_event(task.event_id),
            "transport_departed",
            actor,
            occurred_at,
            task_id=task_id,
        )

    def record_handoff(
        self,
        task_id: str,
        *,
        note: str,
        occurred_at: str,
        actor: Actor,
    ) -> None:
        task = self.get_task(task_id)
        task.phase = TransportPhase.HANDED_OFF
        event = self.get_event(task.event_id)
        event.status = EventStatus.CLOSED
        self._log(event, "handoff_recorded", actor, occurred_at, task_id=task_id, note=note)
        self._notify(event, occurred_at, "hospital", f"事件 {event.event_id} 已完成交接")
        self._emit(
            "HANDOFF_RECORDED",
            "transport_task",
            task_id,
            occurred_at,
            f"事件 {event.event_id} 已完成院前院内交接",
        )

    def update_hospital_capacity(
        self,
        hospital_id: str,
        *,
        accepting: bool,
        occurred_at: str,
        note: str = "",
        actor: Actor,
    ) -> list[str]:
        """更新接诊能力；满负荷时只重算未出发任务的路线，返回被改派的任务。"""
        capacity = self._hospitals.get(hospital_id)
        if capacity is None:
            raise NotFoundError(f"医院不存在: {hospital_id}")
        capacity.accepting = accepting
        capacity.note = note
        capacity.updated_at = occurred_at

        rerouted: list[str] = []
        if accepting:
            return rerouted
        for task in self._tasks.values():
            if task.hospital_id != hospital_id or task.departed:
                continue
            event = self.get_event(task.event_id)
            replacement = self._next_accepting(task.candidate_hospitals, exclude=hospital_id)
            old = task.hospital_id
            task.hospital_id = replacement
            if replacement is None:
                task.phase = TransportPhase.SELECTING
            self._log(
                event,
                "transport_rerouted",
                actor,
                occurred_at,
                task_id=task.task_id,
                from_hospital=old,
                to_hospital=replacement,
                reason=note or "医院临时满负荷",
            )
            if replacement is not None:
                self._notify(
                    event,
                    occurred_at,
                    "hospital",
                    f"事件 {event.event_id} 改送医院 {replacement}",
                )
            rerouted.append(task.task_id)
        return rerouted

    def _next_accepting(self, candidates: list[str], exclude: str) -> str | None:
        for hospital_id in candidates:
            if hospital_id == exclude:
                continue
            capacity = self._hospitals.get(hospital_id)
            if capacity is None or capacity.accepting:
                return hospital_id
        return None

    # ------------------------------------------------------------------
    # 失联与补传
    # ------------------------------------------------------------------

    def advance_transport_local(
        self,
        task_id: str,
        *,
        phase: TransportPhase,
        note: str,
        occurred_at: str,
        actor: Actor,
    ) -> Receipt:
        """急救车失联期间本地继续推进状态，并生成待确认回执。"""
        task = self.get_task(task_id)
        ambulance = self._ambulances.get(task.ambulance_id or "")
        if ambulance is None or ambulance.status != AmbulanceStatus.OFFLINE:
            raise ConflictError("仅急救车失联时允许本地推进")
        receipt = Receipt(
            receipt_id=self._next_id("receipt", "RC"),
            phase=phase,
            note=note,
            occurred_at=occurred_at,
        )
        task.receipts.append(receipt)
        task.phase = phase
        self._log(
            self.get_event(task.event_id),
            "local_progress",
            actor,
            occurred_at,
            task_id=task_id,
            phase=phase.value,
            receipt_id=receipt.receipt_id,
            note=note,
        )
        return receipt

    def ack_receipt(self, task_id: str, receipt_id: str, *, occurred_at: str) -> None:
        task = self.get_task(task_id)
        for index, receipt in enumerate(task.receipts):
            if receipt.receipt_id == receipt_id:
                task.receipts[index] = Receipt(
                    receipt_id=receipt.receipt_id,
                    phase=receipt.phase,
                    note=receipt.note,
                    occurred_at=receipt.occurred_at,
                    acked=True,
                )
                return
        raise NotFoundError(f"回执不存在: {receipt_id}")

    def resync(self, task_id: str, *, occurred_at: str) -> list[Receipt]:
        """连接恢复后只补传未确认的回执。"""
        task = self.get_task(task_id)
        pending = task.pending_receipts()
        if pending:
            self._log(
                self.get_event(task.event_id),
                "resync",
                Actor("system", Role.SYSTEM),
                occurred_at,
                task_id=task_id,
                receipt_ids=[receipt.receipt_id for receipt in pending],
            )
        return pending

    # ------------------------------------------------------------------
    # 可见性与复盘
    # ------------------------------------------------------------------

    def view_event(self, event_id: str, *, role: Role) -> dict[str, Any]:
        """按角色最小可见返回事件视图；未授权定位时不返回位置。"""
        event = self.get_event(event_id)
        full: dict[str, Any] = {
            "event_id": event.event_id,
            "status": event.status.value,
            "triage_level": event.effective_level.value,
            "symptoms": [entry.verbatim for entry in event.symptoms],
            "onset_at": event.onset_at,
            "location": event.location,
            "medical_history": list(event.risk_history),
            "medications": [
                {"name": med.name, "dose": med.dose, "note": med.note}
                for med in event.medications
            ],
            "guidance": [item.text for item in event.guidance],
            "family_confirmations": [
                {"confirmer": item.confirmer, "content": item.content}
                for item in event.family_confirmations
            ],
            "transport": self._transport_view(event),
            "audit": self.audit_trail(event_id),
        }
        if not event.location_consent:
            full.pop("location")
        allowed = ROLE_VISIBLE_FIELDS[role]
        return {key: value for key, value in full.items() if key in allowed}

    def _transport_view(self, event: EmergencyEvent) -> dict[str, Any] | None:
        if event.transport_task_id is None:
            return None
        task = self._tasks[event.transport_task_id]
        return {
            "task_id": task.task_id,
            "phase": task.phase.value,
            "ambulance_id": task.ambulance_id,
            "hospital_id": task.hospital_id,
        }

    def audit_trail(self, event_id: str) -> list[dict[str, Any]]:
        """复盘时间线：还原每个判断、通知与交接时刻；系统提示标注非诊断结论。"""
        event = self.get_event(event_id)
        trail: list[dict[str, Any]] = []
        for entry in event.audit:
            detail = dict(entry.detail)
            if entry.kind == "assessment" and detail.get("source") == AssessmentSource.SYSTEM_HINT.value:
                detail.setdefault("nature", SYSTEM_HINT_DISCLAIMER)
            trail.append(
                {
                    "seq": entry.seq,
                    "occurred_at": entry.occurred_at,
                    "kind": entry.kind,
                    "actor_id": entry.actor_id,
                    "detail": detail,
                }
            )
        return trail
