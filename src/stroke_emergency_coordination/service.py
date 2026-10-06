"""急症预警转运协同应用服务。

把事件存储、各聚合、去重合并、容量重算、离线补传和通知回执
串成一条业务流程；所有状态变化都先成为不可变事件，再更新读模型。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from itertools import count
from typing import Any, Sequence

from .access import project_case, RoleView
from .aggregate import EmergencyCase
from .errors import DomainError
from .event import DomainEvent
from .offline import AmbulanceLocalLog
from .store import EventStore
from .transport import Hospital, HospitalCapacity, TransportTask


class CoordinationService:
    def __init__(self) -> None:
        self.store = EventStore()
        self._hospitals: dict[str, HospitalCapacity] = {}
        self._case_index: dict[str, str] = {}  # case_id -> transport_task_id
        self._caller_index: dict[str, str] = {}  # 来电号码 -> case_id
        self._device_index: dict[str, str] = {}  # 设备标识 -> case_id
        self._eta: dict[str, int] = {}  # hospital_id -> 预计到达分钟数
        self.local_logs: dict[str, AmbulanceLocalLog] = {}
        self._sequences: dict[str, count] = defaultdict(lambda: count(1))

    # --- 基础工具 -----------------------------------------------------
    def _make_id(self, scope: str) -> str:
        def factory(kind: str) -> str:
            return f"{scope}-{kind}-{next(self._sequences[scope + ':' + kind]):03d}"

        return factory  # type: ignore[return-value]

    def _load_case(self, case_id: str) -> EmergencyCase:
        return EmergencyCase.replay(case_id, self.store.load(case_id), self._make_id(case_id))

    def _commit(self, case: EmergencyCase) -> tuple[DomainEvent, ...]:
        events = tuple(case.pending)
        case.pending.clear()
        self.store.append(*events)
        return events

    def _load_task(self, task_id: str) -> TransportTask:
        return TransportTask.replay(task_id, self.store.load(task_id), self._make_id(task_id))

    def _commit_task(self, task: TransportTask) -> tuple[DomainEvent, ...]:
        events = tuple(task.pending)
        task.pending.clear()
        self.store.append(*events)
        return events

    # --- 来电登记 / 去重合并 ------------------------------------------
    def register_call(
        self,
        *,
        case_id: str,
        caller_number: str,
        device_id: str | None,
        operator_id: str,
        at: datetime,
        address: str | None = None,
        location_authorized: bool = False,
        duplicate_ref: str | None = None,
    ) -> tuple[EmergencyCase, tuple[DomainEvent, ...]]:
        """登记来电；相同号码或设备的再次上报合并到既有事件。"""
        existing = self._caller_index.get(caller_number)
        if existing is None and device_id:
            existing = self._device_index.get(device_id)
        if existing is not None:
            case = self._load_case(existing)
            other_ref = duplicate_ref or f"inbound:{case_id}"
            if existing == case_id:
                case.record_duplicate_rejected(
                    other_report_id=other_ref, reason="相同来电号码/设备重复上报", at=at
                )
            else:
                case.record_merge(other_case_id=other_ref, at=at)
            return case, self._commit(case)
        case = EmergencyCase(case_id, self._make_id(case_id))
        case.open(
            caller_number=caller_number,
            device_id=device_id,
            operator_id=operator_id,
            at=at,
            address=address,
            location_authorized=location_authorized,
        )
        events = self._commit(case)
        self._caller_index[caller_number] = case_id
        if device_id:
            self._device_index[device_id] = case_id
        return case, events

    # --- 登记资料 -----------------------------------------------------
    def add_report(
        self,
        *,
        case_id: str,
        kind: str,
        text: str,
        source: str,
        operator_id: str,
        at: datetime,
        onset_time: datetime | None = None,
    ) -> tuple[DomainEvent, ...]:
        case = self._load_case(case_id)
        case.report(
            kind=kind,
            text=text,
            source=source,
            operator_id=operator_id,
            at=at,
            onset_time=onset_time,
        )
        produced = self._commit(case)
        # 若本次登记触发锁定，自动启动转运选择与通知
        if any(event.event_type == "RISK_LOCKED" for event in produced):
            self._start_transport_after_lock(case_id, at, produced)
        return produced

    def professional_decision(
        self,
        *,
        case_id: str,
        professional_id: str,
        role: str,
        level: int,
        assessment: str,
        at: datetime,
    ) -> tuple[DomainEvent, ...]:
        from .triage import TriageLevel

        case = self._load_case(case_id)
        case.record_professional_decision(
            professional_id=professional_id,
            role=role,
            level=TriageLevel(level),
            assessment=assessment,
            at=at,
        )
        produced = self._commit(case)
        if any(event.event_type == "RISK_LOCKED" for event in produced):
            self._start_transport_after_lock(case_id, at, produced)
        return produced

    def grant_location(self, *, case_id: str, operator_id: str, scope: str, address: str, at: datetime) -> tuple[DomainEvent, ...]:
        case = self._load_case(case_id)
        case.grant_location(operator_id=operator_id, scope=scope, address=address, at=at)
        return self._commit(case)

    def revoke_location(self, *, case_id: str, operator_id: str, at: datetime) -> tuple[DomainEvent, ...]:
        case = self._load_case(case_id)
        case.revoke_location(operator_id=operator_id, at=at)
        return self._commit(case)

    def acknowledge_family(self, *, case_id: str, family_member: str, at: datetime, channel: str = "phone") -> tuple[DomainEvent, ...]:
        case = self._load_case(case_id)
        case.acknowledge_guidance(family_member=family_member, at=at, channel=channel)
        return self._commit(case)

    def correct_onset_time(
        self,
        *,
        case_id: str,
        operator_id: str,
        operator_role: str,
        new_value: datetime,
        reason: str,
        at: datetime,
    ) -> tuple[DomainEvent, ...]:
        case = self._load_case(case_id)
        case.correct_onset_time(
            operator_id=operator_id,
            operator_role=operator_role,
            new_value=new_value,
            reason=reason,
            at=at,
        )
        return self._commit(case)

    # --- 医院容量 -----------------------------------------------------
    def register_hospital(
        self,
        *,
        hospital_id: str,
        name: str,
        capability: str,
        beds_available: int,
        eta_minutes: int,
        at: datetime,
        operator_id: str = "capacity-admin",
        status: str = "open",
        reason: str = "建册",
    ) -> tuple[DomainEvent, ...]:
        capacity = HospitalCapacity(hospital_id, self._make_id(hospital_id))
        capacity.update(
            name=name,
            capability=capability,
            beds_available=beds_available,
            status=status,
            reason=reason,
            at=at,
            operator_id=operator_id,
        )
        self._hospitals[hospital_id] = capacity
        self._eta[hospital_id] = eta_minutes
        events = tuple(capacity.pending)
        capacity.pending.clear()
        committed = self.store.append(*events)
        # 新医院建册也可能让挂起中的转运开线
        self._refresh_open_routes(at=at, trigger=f"HOSPITAL_REGISTERED:{hospital_id}")
        return committed

    def update_capacity(
        self,
        *,
        hospital_id: str,
        beds_available: int,
        status: str,
        reason: str,
        at: datetime,
        operator_id: str = "capacity-admin",
    ) -> tuple[DomainEvent, ...]:
        if hospital_id not in self._hospitals:
            raise DomainError("hospital_unknown", f"未登记医院 {hospital_id}")
        capacity = HospitalCapacity.replay(
            hospital_id, self.store.load(hospital_id), self._make_id(hospital_id)
        )
        capacity.update(
            name=self._hospitals[hospital_id].name or hospital_id,
            capability=self._hospitals[hospital_id].capability or "none",
            beds_available=beds_available,
            status=status,
            reason=reason,
            at=at,
            operator_id=operator_id,
        )
        self._hospitals[hospital_id] = capacity
        committed = self.store.append(*capacity.pending)
        # 容量每次变化都刷新挂起/未出发的路线（满负荷改派、恢复后补开）
        self._refresh_open_routes(at=at, trigger=f"CAPACITY_CHANGED:{hospital_id}:{status}")
        return committed

    def _hospital_snapshots(self) -> list[Hospital]:
        return [
            Hospital(
                hospital_id=hid,
                name=cap.name or hid,
                capability=cap.capability or "none",
                eta_minutes=self._eta.get(hid, 0),
                beds_available=cap.beds_available,
                status=cap.status,
            )
            for hid, cap in self._hospitals.items()
        ]

    # --- 转运 ---------------------------------------------------------
    def _start_transport_after_lock(
        self, case_id: str, at: datetime, lock_events: Sequence[DomainEvent]
    ) -> None:
        task_id = f"transport-{case_id}"
        self._case_index[case_id] = task_id
        task = TransportTask(task_id, case_id, self._make_id(task_id))
        chosen_id = self._route_or_suspend(task, at=at, trigger="RISK_LOCKED")
        route_event = next(
            (e for e in task.pending if e.event_type in {"ROUTE_PROPOSED", "ROUTE_SUSPENDED"}), None
        )
        self.store.append(*task.pending)
        task.pending.clear()
        locked_event = next(event for event in lock_events if event.event_type == "RISK_LOCKED")
        # 通知与回执落在事件时间线；无医院可送时只通知车队待命，不发医院预告
        case = self._load_case(case_id)
        case.record_notification(
            channel="fleet-radio",
            target="fleet-desk",
            target_role="fleet",
            at=at,
            ref_event_id=locked_event.event_id,
            summary_text="高危锁定：通知车队准备派车",
        )
        if chosen_id is not None and route_event is not None:
            case.record_notification(
                channel="hospital-hotline",
                target=chosen_id,
                target_role="hospital",
                at=at,
                ref_event_id=route_event.event_id,
                summary_text="高危锁定：向首选医院发出接诊预告",
            )
        self._commit(case)

    def _route_or_suspend(self, task: TransportTask, *, at: datetime, trigger: str) -> str | None:
        """对一个任务尝试开线/改派；没有可接诊医院则挂起。返回选中的医院。

        已在 proposed/assigned 且首选医院没有变化时不产生新事件。
        """
        from .transport import rank_hospitals

        ranked = rank_hospitals(self._hospital_snapshots())
        best_id = ranked[0].hospital_id if ranked else None
        if task.status in {"proposed", "assigned"} and best_id == task.hospital_id:
            return best_id  # 首选未变，无需重算
        if task.status == "suspended" and best_id is None:
            return None  # 仍无可接诊医院，维持挂起，不重复落事件
        try:
            task.propose_routes(
                hospitals=self._hospital_snapshots(),
                at=at,
                operator_id="system-dispatch",
                trigger=trigger,
            )
        except DomainError as error:
            if error.code != "no_available_hospital":
                raise
            task.suspend_routes(
                at=at,
                operator_id="system-dispatch",
                trigger=trigger,
                reason="暂无可接诊卒中能力医院，等待容量恢复",
            )
            return None
        return task.pending[-1].payload["chosen_hospital_id"]

    def _refresh_open_routes(self, *, at: datetime, trigger: str) -> None:
        """容量变化后：跳过已出发（路线冻结）；刷新挂起/未出发的任务。

        - 挂起中且现在有医院 → 自动开线，补发医院预告；
        - 未出发且首选医院变化（满负荷）→ 改派；
        - 原来有医院、现在全部满负荷 → 挂起。
        """
        for case_id, task_id in list(self._case_index.items()):
            task = self._load_task(task_id)
            if task.route_frozen or task.status not in {"proposed", "assigned", "suspended"}:
                continue
            previous_hospital = task.hospital_id
            previous_ambulance = task.ambulance_id
            chosen_id = self._route_or_suspend(task, at=at, trigger=trigger)
            # 已派车但未出发，且改派到别的医院：同步更新派车指向
            if (
                previous_ambulance
                and chosen_id is not None
                and chosen_id != previous_hospital
                and task.status == "proposed"
            ):
                task.assign_ambulance(
                    ambulance_id=previous_ambulance,
                    hospital_id=chosen_id,
                    at=at,
                    operator_id="system-dispatch",
                )
            route_events = tuple(task.pending)
            task.pending.clear()
            if not route_events:
                continue
            self.store.append(*route_events)
            # 容量恢复后新开线 / 满负荷改派：通知新医院
            if chosen_id is not None and chosen_id != previous_hospital:
                case = self._load_case(case_id)
                case.record_notification(
                    channel="hospital-hotline",
                    target=chosen_id,
                    target_role="hospital",
                    at=at,
                    ref_event_id=route_events[0].event_id,
                    summary_text=(
                        "容量恢复：挂起任务开线，向医院发出接诊预告"
                        if previous_hospital is None
                        else "首选医院满负荷：改派本院并发出接诊预告"
                    ),
                )
                self._commit(case)

    def assign_ambulance(self, *, case_id: str, ambulance_id: str, at: datetime, operator_id: str) -> tuple[DomainEvent, ...]:
        task = self._load_task(self._require_task_id(case_id))
        chosen = task.hospital_id
        if not chosen:
            raise DomainError("no_route", "尚无候选路线")
        task.assign_ambulance(ambulance_id=ambulance_id, hospital_id=chosen, at=at, operator_id=operator_id)
        events = self._commit_task(task)
        self.local_logs.setdefault(ambulance_id, AmbulanceLocalLog(ambulance_id, self._make_id(ambulance_id)))
        return events

    def depart(self, *, case_id: str, at: datetime, operator_id: str) -> tuple[DomainEvent, ...]:
        task = self._load_task(self._require_task_id(case_id))
        task.depart(at=at, operator_id=operator_id)
        return self._commit_task(task)

    def arrive(self, *, case_id: str, at: datetime, phase: str, operator_id: str) -> tuple[DomainEvent, ...]:
        task = self._load_task(self._require_task_id(case_id))
        task.arrive(at=at, phase=phase, operator_id=operator_id)
        return self._commit_task(task)

    def ambulance_offline(self, *, case_id: str, at: datetime, note: str = "") -> tuple[DomainEvent, ...]:
        task = self._load_task(self._require_task_id(case_id))
        task.mark_offline(at=at, note=note)
        return self._commit_task(task)

    def ambulance_reconnect(self, *, case_id: str, at: datetime) -> tuple[DomainEvent, ...]:
        """恢复连接：把车载本地日志中尚无确认回执的事件补传，再登记重连。"""
        task_id = self._require_task_id(case_id)
        task = self._load_task(task_id)
        ambulance_id = task.ambulance_id or ""
        log = self.local_logs.get(ambulance_id)
        synced_ids: list[str] = []
        if log is not None:
            pending = log.pending_sync()
            committed = self.sync_ambulance_events(pending)
            synced_ids = [event.event_id for event in committed]
            log.mark_all_synced(synced_ids)
        # 补传已追加进事件流，重新重放后再产生重连事件以保证版本连续
        task = self._load_task(task_id)
        task.mark_reconnected(at=at, synced_event_ids=synced_ids)
        return self._commit_task(task)

    def offline_arrive(self, *, case_id: str, at: datetime, phase: str) -> tuple[DomainEvent, ...]:
        """失联期间的本地推进：事件只写车载本地日志，不进中心存储。"""
        task = self._load_task(self._require_task_id(case_id))
        ambulance_id = task.ambulance_id or ""
        task.arrive(at=at, phase=phase, operator_id=ambulance_id)
        log = self.local_logs.setdefault(
            ambulance_id, AmbulanceLocalLog(ambulance_id, self._make_id(ambulance_id))
        )
        produced = tuple(task.pending)
        task.pending.clear()
        for event in produced:
            log.record_local(event)
        return produced

    def sync_ambulance_events(self, events: Sequence[DomainEvent]) -> tuple[DomainEvent, ...]:
        """恢复连接后的补传入口：只追加服务端尚未持有（=未确认回执）的事件。

        已存在的 event_id 直接跳过并视为已确认，重传永远幂等。
        """
        committed: list[DomainEvent] = []
        for event in events:
            if self.store.exists(event.event_id):
                continue
            self.store.append(event)
            committed.append(event)
        return tuple(committed)

    def hospital_accept(self, *, case_id: str, hospital_id: str, at: datetime, operator_id: str) -> tuple[DomainEvent, ...]:
        task = self._load_task(self._require_task_id(case_id))
        task.hospital_accept(hospital_id=hospital_id, at=at, operator_id=operator_id)
        return self._commit_task(task)

    def record_handoff(self, *, case_id: str, at: datetime, to_staff: str, from_staff: str, vital_signs: dict[str, Any], note: str = "") -> tuple[DomainEvent, ...]:
        task = self._load_task(self._require_task_id(case_id))
        task.record_handoff(
            at=at, to_staff=to_staff, from_staff=from_staff, vital_signs=vital_signs, note=note
        )
        return self._commit_task(task)

    def acknowledge_notification(self, *, case_id: str, notification_event_id: str, at: datetime, actor: str) -> tuple[DomainEvent, ...]:
        case = self._load_case(case_id)
        case.acknowledge_receipt(notification_event_id=notification_event_id, at=at, actor=actor)
        return self._commit(case)

    # --- 读模型 -------------------------------------------------------
    def view_case(self, case_id: str, role: str) -> RoleView:
        return project_case(self._load_case(case_id), role)

    def case_events(self, case_id: str) -> tuple[DomainEvent, ...]:
        return self.store.load(case_id)

    def case_timeline_events(self, case_id: str) -> tuple[DomainEvent, ...]:
        """复盘取数：事件流 + 该事件的转运流，保持全局提交顺序。"""
        task_id = self._case_index.get(case_id)
        ids = {case_id, task_id} - {None}
        return tuple(event for event in self.store.all_events() if event.aggregate_id in ids)

    def _require_task_id(self, case_id: str) -> str:
        task_id = self._case_index.get(case_id)
        if not task_id:
            raise DomainError("transport_not_started", f"事件 {case_id} 尚未启动转运")
        return task_id
