"""转运任务、急救车与医院容量。

关键规则：
- 事件锁定后才启动转运选择，候选医院按卒中接诊能力、实时容量、预计到达时间排序；
- 医院临时满负荷时，只重算"尚未出发"的路线；急救车一旦出发，
  路线与目的地冻结，容量变化不再影响该任务；
- 医院满负荷是临时状态，恢复后自动重新进入候选集。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .errors import DomainError
from .event import DomainEvent, ensure_aware

TRANSPORT_AGGREGATE = "transport_task"
CAPACITY_AGGREGATE = "hospital_capacity"

IdFactory = Callable[[str], str]

CAPABILITY_RANK = {"comprehensive": 0, "primary": 1, "basic": 2, "none": 3}


@dataclass(frozen=True)
class Hospital:
    hospital_id: str
    name: str
    capability: str  # comprehensive / primary / basic / none
    eta_minutes: int
    beds_available: int
    status: str = "open"  # open / full / bypassing

    @property
    def can_accept(self) -> bool:
        return self.status == "open" and self.beds_available > 0

    def as_candidate(self) -> dict[str, Any]:
        return {
            "hospital_id": self.hospital_id,
            "name": self.name,
            "capability": self.capability,
            "eta_minutes": self.eta_minutes,
            "beds_available": self.beds_available,
            "status": self.status,
            "can_accept": self.can_accept,
        }


def rank_hospitals(hospitals: Sequence[Hospital]) -> list[Hospital]:
    """可接诊的医院中：卒中能力优先，再比预计到达时间，再比空床数。"""
    usable = [hospital for hospital in hospitals if hospital.can_accept]
    return sorted(
        usable,
        key=lambda h: (CAPABILITY_RANK.get(h.capability, 9), h.eta_minutes, -h.beds_available),
    )


@dataclass
class RouteProposal:
    at: datetime
    candidates: tuple[dict[str, Any], ...]
    chosen_hospital_id: str | None
    reason: str
    recalculated: bool


class TransportTask:
    def __init__(self, task_id: str, case_id: str, next_id: IdFactory) -> None:
        self.task_id = task_id
        self.case_id = case_id
        self._next_id = next_id
        self._version = 0
        self.pending: list[DomainEvent] = []

        self.started = False
        self.status = "new"  # new/proposed/suspended/assigned/departed/arrived_scene/arrived_hospital/handed_off
        self.ambulance_id: str | None = None
        self.hospital_id: str | None = None
        self.proposals: list[RouteProposal] = []
        self.suspensions: list[Mapping[str, Any]] = []
        self.departed_at: datetime | None = None
        self.offline = False
        self.handoff: Mapping[str, Any] | None = None

    @classmethod
    def replay(cls, task_id: str, events: Sequence[DomainEvent], next_id: IdFactory) -> "TransportTask":
        # case_id 从首条事件 payload 恢复
        case_id = events[0].payload.get("case_id", "") if events else ""
        task = cls(task_id, case_id, next_id)
        for event in events:
            task.apply(event)
        task.pending.clear()
        return task

    def apply(self, event: DomainEvent) -> None:
        self._version = event.version
        p = event.payload
        kind = event.event_type
        if kind in ("ROUTE_PROPOSED", "ROUTE_RECALCULATED"):
            self.started = True
            self.status = "proposed"
            self.proposals.append(
                RouteProposal(
                    at=event.occurred_at,
                    candidates=tuple(p.get("candidates", ())),
                    chosen_hospital_id=p.get("chosen_hospital_id"),
                    reason=p.get("reason", ""),
                    recalculated=kind == "ROUTE_RECALCULATED",
                )
            )
            if p.get("chosen_hospital_id"):
                self.hospital_id = p["chosen_hospital_id"]
        elif kind == "ROUTE_SUSPENDED":
            self.status = "suspended"
            self.suspensions.append(
                {"at": event.occurred_at.isoformat(), "reason": p.get("reason", ""), "trigger": p.get("trigger", "")}
            )
        elif kind == "TRANSPORT_ASSIGNED":
            self.status = "assigned"
            self.ambulance_id = p["ambulance_id"]
            self.hospital_id = p["hospital_id"]
        elif kind == "AMBULANCE_DEPARTED":
            self.status = "departed"
            self.departed_at = event.occurred_at
        elif kind == "AMBULANCE_ARRIVED":
            self.status = "arrived_hospital" if p.get("phase") == "hospital" else "arrived_scene"
        elif kind == "AMBULANCE_OFFLINE":
            self.offline = True
        elif kind == "AMBULANCE_RECONNECTED":
            self.offline = False
        elif kind == "HOSPITAL_ACCEPTED":
            self.hospital_id = p["hospital_id"]
        elif kind == "HANDOFF_RECORDED":
            self.status = "handed_off"
            self.handoff = dict(p, handed_at=event.occurred_at.isoformat())

    def _emit(
        self,
        event_type: str,
        summary: str,
        payload: Mapping[str, Any],
        at: datetime,
        *,
        operator_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> DomainEvent:
        ensure_aware(at)
        event = DomainEvent(
            event_id=self._next_id(event_type.lower()),
            event_type=event_type,
            aggregate_type=TRANSPORT_AGGREGATE,
            aggregate_id=self.task_id,
            occurred_at=at,
            version=self._version + 1,
            summary=summary,
            payload=dict(payload),
            operator_id=operator_id,
            correlation_id=correlation_id or self.case_id,
            causation_id=causation_id,
        )
        self.pending.append(event)
        self.apply(event)
        return event

    @property
    def route_frozen(self) -> bool:
        """出发即冻结：满负荷重算不得影响本任务。"""
        return self.departed_at is not None

    def suspend_routes(self, *, at: datetime, operator_id: str, trigger: str, reason: str) -> DomainEvent:
        if self.route_frozen:
            raise DomainError("route_frozen", "已出发的任务不能挂起")
        return self._emit(
            "ROUTE_SUSPENDED",
            "暂无可接诊医院，转运选择挂起，等待容量恢复后自动开线",
            {"case_id": self.case_id, "trigger": trigger, "reason": reason},
            at,
            operator_id=operator_id,
        )

    def propose_routes(
        self,
        *,
        hospitals: Sequence[Hospital],
        at: datetime,
        operator_id: str,
        trigger: str,
        recalculated: bool = False,
    ) -> DomainEvent:
        if recalculated and self.route_frozen:
            raise DomainError(
                "route_frozen",
                f"任务 {self.task_id} 已出发，路线冻结，不再因医院容量重算",
            )
        if self.status not in {"new", "suspended", "proposed", "assigned"}:
            raise DomainError("invalid_transition", f"状态 {self.status} 不能提出路线")
        ranked = rank_hospitals(hospitals)
        if not ranked:
            raise DomainError("no_available_hospital", "当前没有可接诊的卒中能力医院")
        chosen = ranked[0]
        payload = {
            "case_id": self.case_id,
            "trigger": trigger,
            "candidates": [hospital.as_candidate() for hospital in ranked],
            "chosen_hospital_id": chosen.hospital_id,
            "reason": f"按卒中接诊能力、预计到达时间（{chosen.eta_minutes}分钟）、空床数排序",
        }
        if self.status == "new":
            event_type, summary = "ROUTE_PROPOSED", "锁定后启动转运选择，给出候选路线"
        elif self.status == "suspended":
            event_type, summary = "ROUTE_PROPOSED", "容量恢复，挂起的转运选择自动开线"
        else:
            event_type, summary = "ROUTE_RECALCULATED", "医院临时满负荷，重算未出发路线"
        return self._emit(event_type, summary, payload, at, operator_id=operator_id)

    def assign_ambulance(
        self, *, ambulance_id: str, hospital_id: str, at: datetime, operator_id: str
    ) -> DomainEvent:
        if self.status not in {"proposed"}:
            raise DomainError("invalid_transition", f"状态 {self.status} 不能派车")
        return self._emit(
            "TRANSPORT_ASSIGNED",
            f"指派急救车 {ambulance_id} 送往 {hospital_id}",
            {
                "case_id": self.case_id,
                "ambulance_id": ambulance_id,
                "hospital_id": hospital_id,
            },
            at,
            operator_id=operator_id,
        )

    def depart(self, *, at: datetime, operator_id: str) -> DomainEvent:
        if self.status != "assigned":
            raise DomainError("invalid_transition", f"状态 {self.status} 不能出发")
        return self._emit(
            "AMBULANCE_DEPARTED",
            "急救车出发，路线冻结",
            {"case_id": self.case_id, "ambulance_id": self.ambulance_id},
            at,
            operator_id=operator_id,
        )

    def arrive(self, *, at: datetime, phase: str, operator_id: str) -> DomainEvent:
        if phase not in {"scene", "hospital"}:
            raise DomainError("unsupported_phase", "phase 只能是 scene 或 hospital")
        if phase == "scene" and self.status != "departed":
            raise DomainError("invalid_transition", "尚未出发，不能到达现场")
        if phase == "hospital" and self.status != "arrived_scene":
            raise DomainError("invalid_transition", "未到过现场，不能到达医院")
        return self._emit(
            "AMBULANCE_ARRIVED",
            f"急救车到达{'现场' if phase == 'scene' else '医院'}",
            {"case_id": self.case_id, "phase": phase, "ambulance_id": self.ambulance_id},
            at,
            operator_id=operator_id,
        )

    def mark_offline(self, *, at: datetime, note: str = "") -> DomainEvent:
        return self._emit(
            "AMBULANCE_OFFLINE",
            "急救车失联，现场/车载状态本地继续推进",
            {"case_id": self.case_id, "ambulance_id": self.ambulance_id, "note": note},
            at,
            operator_id=self.ambulance_id,
        )

    def mark_reconnected(self, *, at: datetime, synced_event_ids: Sequence[str]) -> DomainEvent:
        return self._emit(
            "AMBULANCE_RECONNECTED",
            "通信恢复，仅补传尚无确认回执的事件",
            {
                "case_id": self.case_id,
                "ambulance_id": self.ambulance_id,
                "synced_event_ids": list(synced_event_ids),
            },
            at,
            operator_id=self.ambulance_id,
        )

    def hospital_accept(
        self, *, hospital_id: str, at: datetime, operator_id: str, note: str = ""
    ) -> DomainEvent:
        return self._emit(
            "HOSPITAL_ACCEPTED",
            f"医院 {hospital_id} 确认接诊",
            {"case_id": self.case_id, "hospital_id": hospital_id, "note": note},
            at,
            operator_id=operator_id,
        )

    def record_handoff(
        self,
        *,
        at: datetime,
        to_staff: str,
        from_staff: str,
        vital_signs: Mapping[str, Any],
        note: str = "",
    ) -> DomainEvent:
        if self.status != "arrived_hospital":
            raise DomainError("invalid_transition", f"状态 {self.status} 不能记录院内交接")
        return self._emit(
            "HANDOFF_RECORDED",
            f"与 {to_staff} 完成院内交接",
            {
                "case_id": self.case_id,
                "hospital_id": self.hospital_id,
                "from_staff": from_staff,
                "to_staff": to_staff,
                "vital_signs": dict(vital_signs),
                "note": note,
            },
            at,
            operator_id=from_staff,
        )

    @property
    def version(self) -> int:
        return self._version


class HospitalCapacity:
    """医院接诊能力聚合：能力等级相对稳定，床位与满负荷状态随事件变化。"""

    def __init__(self, hospital_id: str, next_id: IdFactory) -> None:
        self.hospital_id = hospital_id
        self._next_id = next_id
        self._version = 0
        self.pending: list[DomainEvent] = []
        self.name: str | None = None
        self.capability: str | None = None
        self.beds_available = 0
        self.status = "open"
        self.history: list[Mapping[str, Any]] = []

    @classmethod
    def replay(cls, hospital_id: str, events: Sequence[DomainEvent], next_id: IdFactory) -> "HospitalCapacity":
        capacity = cls(hospital_id, next_id)
        for event in events:
            capacity.apply(event)
        capacity.pending.clear()
        return capacity

    def apply(self, event: DomainEvent) -> None:
        self._version = event.version
        p = event.payload
        if event.event_type == "HOSPITAL_CAPACITY_CHANGED":
            self.name = p.get("name", self.name)
            self.capability = p.get("capability", self.capability)
            self.beds_available = int(p["beds_available"])
            self.status = p["status"]
            self.history.append(
                {
                    "at": event.occurred_at.isoformat(),
                    "beds_available": self.beds_available,
                    "status": self.status,
                    "reason": p.get("reason", ""),
                }
            )

    def _emit(self, payload: Mapping[str, Any], at: datetime, operator_id: str, summary: str) -> DomainEvent:
        ensure_aware(at)
        event = DomainEvent(
            event_id=self._next_id("hospital_capacity_changed"),
            event_type="HOSPITAL_CAPACITY_CHANGED",
            aggregate_type=CAPACITY_AGGREGATE,
            aggregate_id=self.hospital_id,
            occurred_at=at,
            version=self._version + 1,
            summary=summary,
            payload=dict(payload),
            operator_id=operator_id,
        )
        self.pending.append(event)
        self.apply(event)
        return event

    def update(
        self,
        *,
        at: datetime,
        operator_id: str,
        name: str,
        capability: str,
        beds_available: int,
        status: str,
        reason: str,
    ) -> DomainEvent:
        if status not in {"open", "full", "bypassing"}:
            raise DomainError("unsupported_status", f"未知容量状态：{status}")
        if capability not in CAPABILITY_RANK:
            raise DomainError("unsupported_capability", f"未知能力等级：{capability}")
        if beds_available < 0:
            raise DomainError("invalid_beds", "空床数不能为负")
        return self._emit(
            {
                "name": name,
                "capability": capability,
                "beds_available": beds_available,
                "status": status,
                "reason": reason,
            },
            at,
            operator_id,
            f"医院 {name} 容量更新：{status}，空床 {beds_available}",
        )

    def snapshot(self) -> Hospital:
        return Hospital(
            hospital_id=self.hospital_id,
            name=self.name or self.hospital_id,
            capability=self.capability or "none",
            eta_minutes=0,  # 由车队/路况侧提供，容量聚合不掌握距离
            beds_available=self.beds_available,
            status=self.status,
        )

    @property
    def version(self) -> int:
        return self._version
