from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stroke_emergency_coordination.contracts import validate_event
from stroke_emergency_coordination.models import (
    SYSTEM_HINT_DISCLAIMER,
    STANDARD_GUIDANCE,
    Actor,
    AmbulanceStatus,
    AssessmentSource,
    EventStatus,
    Role,
    TransportPhase,
    TriageLevel,
)
from stroke_emergency_coordination.service import (
    ConflictError,
    CoordinationService,
    PermissionDeniedError,
)

T0 = "2026-10-06T08:00:00+08:00"
T1 = "2026-10-06T08:05:00+08:00"
T2 = "2026-10-06T08:10:00+08:00"
T3 = "2026-10-06T08:15:00+08:00"
T4 = "2026-10-06T08:20:00+08:00"
T5 = "2026-10-06T08:25:00+08:00"
T6 = "2026-10-06T08:30:00+08:00"

DISPATCHER = Actor("op-01", Role.DISPATCHER)
SUPERVISOR = Actor("sv-01", Role.SUPERVISOR)
MEDIC = Actor("md-01", Role.MEDIC)
HOSPITAL = Actor("hs-01", Role.HOSPITAL_STAFF)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))

    def make_service(self) -> CoordinationService:
        service = CoordinationService(self.schema)
        service.register_hospital("H1", name="第一医院", occurred_at=T0)
        service.register_hospital("H2", name="第二医院", occurred_at=T0)
        service.register_ambulance("A1", occurred_at=T0)
        service.register_ambulance("A2", occurred_at=T0)
        service.register_triage_rule(
            rule_id="R1",
            description="疑似卒中高危信号",
            keywords=["剧烈头痛", "呕吐", "言语含糊"],
            threshold=2,
        )
        return service

    def locked_event(self, service: CoordinationService, caller: str = "C1") -> str:
        result = service.register_call(
            caller_id=caller, device_id=None, occurred_at=T0, summary="家属来电", actor=DISPATCHER
        )
        service.record_symptom(
            result.event_id,
            verbatim="他突然剧烈头痛，还呕吐，说话言语含糊",
            occurred_at=T1,
            actor=DISPATCHER,
        )
        service.evaluate_rules(result.event_id, occurred_at=T2)
        return result.event_id


class RegistrationTests(Base):
    def test_same_caller_within_window_is_merged(self) -> None:
        service = self.make_service()
        first = service.register_call(
            caller_id="C1", device_id="D1", occurred_at=T0, summary="首次来电", actor=DISPATCHER
        )
        again = service.register_call(
            caller_id="C1", device_id="D9", occurred_at=T3, summary="再次来电", actor=DISPATCHER
        )
        self.assertFalse(first.merged)
        self.assertTrue(again.merged)
        self.assertEqual(first.event_id, again.event_id)
        kinds = [entry["kind"] for entry in service.audit_trail(first.event_id)]
        self.assertIn("duplicate_merged", kinds)

    def test_same_device_is_merged_and_late_call_creates_new_event(self) -> None:
        service = self.make_service()
        first = service.register_call(
            caller_id="C1", device_id="D1", occurred_at=T0, summary="首次", actor=DISPATCHER
        )
        by_device = service.register_call(
            caller_id="C2", device_id="D1", occurred_at=T1, summary="同设备", actor=DISPATCHER
        )
        self.assertTrue(by_device.merged)
        late = service.register_call(
            caller_id="C1",
            device_id=None,
            occurred_at="2026-10-06T09:00:00+08:00",
            summary="超出合并窗口",
            actor=DISPATCHER,
        )
        self.assertFalse(late.merged)
        self.assertNotEqual(first.event_id, late.event_id)


class OnsetTimeTests(Base):
    def test_conflict_requires_supervisor_and_keeps_original(self) -> None:
        service = self.make_service()
        event_id = service.register_call(
            caller_id="C1", device_id=None, occurred_at=T0, summary="来电", actor=DISPATCHER
        ).event_id
        service.record_onset(event_id, onset_at="2026-10-06T07:30:00+08:00", occurred_at=T1, actor=DISPATCHER)
        with self.assertRaises(ConflictError):
            service.record_onset(
                event_id, onset_at="2026-10-06T07:45:00+08:00", occurred_at=T2, actor=DISPATCHER
            )
        with self.assertRaises(PermissionDeniedError):
            service.correct_onset_time(
                event_id,
                new_onset_at="2026-10-06T07:45:00+08:00",
                reason="接线员尝试更正",
                occurred_at=T3,
                actor=DISPATCHER,
            )
        service.correct_onset_time(
            event_id,
            new_onset_at="2026-10-06T07:45:00+08:00",
            reason="家属回忆后确认",
            occurred_at=T4,
            actor=SUPERVISOR,
        )
        event = service.get_event(event_id)
        self.assertEqual("2026-10-06T07:45:00+08:00", event.onset_at)
        self.assertEqual("2026-10-06T07:30:00+08:00", event.onset_corrections[0].original)


class AssessmentTests(Base):
    def test_threshold_locks_event_with_guidance_and_transport(self) -> None:
        service = self.make_service()
        event_id = self.locked_event(service)
        event = service.get_event(event_id)
        self.assertEqual(EventStatus.LOCKED, event.status)
        self.assertEqual(TriageLevel.CRITICAL, event.locked_level)
        self.assertEqual(list(STANDARD_GUIDANCE), [item.text for item in event.guidance])
        task = service.get_task(event.transport_task_id)
        self.assertEqual(TransportPhase.SELECTING, task.phase)
        self.assertEqual(["H1", "H2"], task.candidate_hospitals)
        trail = service.audit_trail(event_id)
        notifications = [e for e in trail if e["kind"] == "notification"]
        self.assertTrue(any(n["detail"]["channel"] == "family" for n in notifications))
        types = [e["event_type"] for e in service.exchange_events]
        self.assertEqual(["CALL_RECEIVED", "RISK_LOCKED"], types)

    def test_system_hint_and_professional_judgment_are_separate(self) -> None:
        service = self.make_service()
        event_id = self.locked_event(service)
        service.assess(
            event_id,
            source=AssessmentSource.PROFESSIONAL,
            level=TriageLevel.URGENT,
            signals=["现场查体"],
            actor=MEDIC,
            occurred_at=T3,
        )
        event = service.get_event(event_id)
        self.assertEqual(TriageLevel.CRITICAL, event.system_level)
        self.assertEqual(TriageLevel.URGENT, event.professional_level)
        self.assertEqual(TriageLevel.CRITICAL, event.effective_level)
        trail = service.audit_trail(event_id)
        hints = [
            e for e in trail
            if e["kind"] == "assessment" and e["detail"]["source"] == "system_hint"
        ]
        self.assertEqual(SYSTEM_HINT_DISCLAIMER, hints[0]["detail"]["nature"])

    def test_assessment_roles_are_enforced(self) -> None:
        service = self.make_service()
        event_id = service.register_call(
            caller_id="C1", device_id=None, occurred_at=T0, summary="来电", actor=DISPATCHER
        ).event_id
        with self.assertRaises(PermissionDeniedError):
            service.assess(
                event_id,
                source=AssessmentSource.PROFESSIONAL,
                level=TriageLevel.URGENT,
                signals=[],
                actor=DISPATCHER,
                occurred_at=T1,
            )
        with self.assertRaises(PermissionDeniedError):
            service.assess(
                event_id,
                source=AssessmentSource.SYSTEM_HINT,
                level=TriageLevel.CRITICAL,
                signals=[],
                actor=SUPERVISOR,
                occurred_at=T1,
            )

    def test_supplement_adds_context_but_never_downgrades(self) -> None:
        service = self.make_service()
        event_id = self.locked_event(service)
        service.add_supplement(
            event_id,
            note="家属补充：患者刚才似乎好转",
            occurred_at=T3,
            actor=DISPATCHER,
            suggested_level=TriageLevel.ROUTINE,
        )
        event = service.get_event(event_id)
        self.assertEqual(TriageLevel.CRITICAL, event.effective_level)
        trail = service.audit_trail(event_id)
        supplements = [e for e in trail if e["kind"] == "supplement_added"]
        self.assertEqual("critical", supplements[0]["detail"]["level_floor_kept"])
        self.assertEqual(1, len(event.supplements))

    def test_supplement_can_escalate_and_trigger_lock(self) -> None:
        service = self.make_service()
        event_id = service.register_call(
            caller_id="C1", device_id=None, occurred_at=T0, summary="来电", actor=DISPATCHER
        ).event_id
        service.assess(
            event_id,
            source=AssessmentSource.PROFESSIONAL,
            level=TriageLevel.URGENT,
            signals=["单侧肢体无力"],
            actor=SUPERVISOR,
            occurred_at=T1,
        )
        self.assertEqual(EventStatus.OPEN, service.get_event(event_id).status)
        service.add_supplement(
            event_id,
            note="补充病史：房颤且已停药",
            occurred_at=T2,
            actor=DISPATCHER,
            suggested_level=TriageLevel.CRITICAL,
        )
        self.assertEqual(EventStatus.LOCKED, service.get_event(event_id).status)


class TransportTests(Base):
    def test_overload_reroutes_only_not_departed(self) -> None:
        service = self.make_service()
        event1 = self.locked_event(service, caller="C1")
        event2 = self.locked_event(service, caller="C2")
        task1 = service.assign_transport(
            event1, ambulance_id="A1", hospital_id="H1", occurred_at=T3, actor=SUPERVISOR
        )
        task2 = service.assign_transport(
            event2, ambulance_id="A2", hospital_id="H1", occurred_at=T3, actor=SUPERVISOR
        )
        service.depart(task1.task_id, occurred_at=T4, actor=MEDIC)
        rerouted = service.update_hospital_capacity(
            "H1", accepting=False, occurred_at=T5, note="急诊满床", actor=HOSPITAL
        )
        self.assertEqual([task2.task_id], rerouted)
        self.assertEqual("H1", service.get_task(task1.task_id).hospital_id)
        self.assertEqual("H2", service.get_task(task2.task_id).hospital_id)

    def test_overload_without_alternative_returns_to_selecting(self) -> None:
        service = CoordinationService(self.schema)
        service.register_hospital("H1", name="第一医院", occurred_at=T0)
        service.register_ambulance("A1", occurred_at=T0)
        service.register_triage_rule(
            rule_id="R1", description="高危", keywords=["剧烈头痛", "呕吐"], threshold=2
        )
        event_id = self.locked_event(service)
        task = service.assign_transport(
            event_id, ambulance_id="A1", hospital_id="H1", occurred_at=T3, actor=SUPERVISOR
        )
        rerouted = service.update_hospital_capacity(
            "H1", accepting=False, occurred_at=T4, note="满床", actor=HOSPITAL
        )
        self.assertEqual([task.task_id], rerouted)
        task = service.get_task(task.task_id)
        self.assertIsNone(task.hospital_id)
        self.assertEqual(TransportPhase.SELECTING, task.phase)

    def test_offline_progress_resyncs_only_unacked_receipts(self) -> None:
        service = self.make_service()
        event_id = self.locked_event(service)
        task = service.assign_transport(
            event_id, ambulance_id="A1", hospital_id="H1", occurred_at=T3, actor=SUPERVISOR
        )
        with self.assertRaises(ConflictError):
            service.advance_transport_local(
                task.task_id, phase=TransportPhase.DEPARTED, note="在线时禁止", occurred_at=T4, actor=MEDIC
            )
        service.update_ambulance_status("A1", status=AmbulanceStatus.OFFLINE, occurred_at=T4)
        first = service.advance_transport_local(
            task.task_id, phase=TransportPhase.DEPARTED, note="本地记录出发", occurred_at=T5, actor=MEDIC
        )
        second = service.advance_transport_local(
            task.task_id, phase=TransportPhase.ARRIVED, note="本地记录到达", occurred_at=T6, actor=MEDIC
        )
        self.assertEqual(TransportPhase.ARRIVED, service.get_task(task.task_id).phase)
        service.ack_receipt(task.task_id, first.receipt_id, occurred_at=T6)
        pending = service.resync(task.task_id, occurred_at="2026-10-06T08:40:00+08:00")
        self.assertEqual([second.receipt_id], [receipt.receipt_id for receipt in pending])


class VisibilityTests(Base):
    def test_location_and_history_follow_least_visibility(self) -> None:
        service = self.make_service()
        event_id = service.register_call(
            caller_id="C1", device_id=None, occurred_at=T0, summary="来电", actor=DISPATCHER
        ).event_id
        service.record_symptom(event_id, verbatim="剧烈头痛", occurred_at=T1, actor=DISPATCHER)
        service.record_risk_history(event_id, items=["高血压", "房颤"], occurred_at=T1, actor=DISPATCHER)
        service.record_medication(
            event_id, name="华法林", dose="3mg", note="已停药一周", occurred_at=T1, actor=DISPATCHER
        )
        service.authorize_location(
            event_id, granted=True, location="某小区3栋", occurred_at=T1, actor=DISPATCHER
        )
        dispatcher_view = service.view_event(event_id, role=Role.DISPATCHER)
        self.assertEqual("某小区3栋", dispatcher_view["location"])
        self.assertNotIn("medical_history", dispatcher_view)
        self.assertNotIn("medications", dispatcher_view)
        medic_view = service.view_event(event_id, role=Role.MEDIC)
        self.assertEqual(["高血压", "房颤"], medic_view["medical_history"])
        hospital_view = service.view_event(event_id, role=Role.HOSPITAL_STAFF)
        self.assertNotIn("location", hospital_view)
        self.assertIn("medical_history", hospital_view)
        auditor_view = service.view_event(event_id, role=Role.AUDITOR)
        self.assertEqual(
            {"event_id", "status", "triage_level", "audit"}, set(auditor_view.keys())
        )

    def test_location_hidden_without_consent(self) -> None:
        service = self.make_service()
        event_id = service.register_call(
            caller_id="C1", device_id=None, occurred_at=T0, summary="来电", actor=DISPATCHER
        ).event_id
        service.authorize_location(
            event_id, granted=False, location="不应保存", occurred_at=T1, actor=DISPATCHER
        )
        self.assertNotIn("location", service.view_event(event_id, role=Role.MEDIC))
        self.assertIsNone(service.get_event(event_id).location)


class AuditReplayTests(Base):
    def test_replay_restores_judgments_notifications_and_handoff(self) -> None:
        service = self.make_service()
        event_id = self.locked_event(service)
        service.confirm_by_family(
            event_id, confirmer="家属", content="已知晓禁忌指导", occurred_at=T3, actor=DISPATCHER
        )
        task = service.assign_transport(
            event_id, ambulance_id="A1", hospital_id="H1", occurred_at=T3, actor=SUPERVISOR
        )
        service.hospital_accept(task.task_id, occurred_at=T4, actor=HOSPITAL)
        service.depart(task.task_id, occurred_at=T5, actor=MEDIC)
        service.record_handoff(task.task_id, note="交接完成", occurred_at=T6, actor=MEDIC)

        trail = service.audit_trail(event_id)
        self.assertEqual(list(range(1, len(trail) + 1)), [e["seq"] for e in trail])
        kinds = [e["kind"] for e in trail]
        for expected in (
            "call_registered",
            "symptom_recorded",
            "assessment",
            "risk_locked",
            "guidance_issued",
            "notification",
            "family_confirmation",
            "transport_assigned",
            "hospital_accepted",
            "transport_departed",
            "handoff_recorded",
        ):
            self.assertIn(expected, kinds)
        self.assertTrue(all(e["occurred_at"] for e in trail))
        self.assertEqual(EventStatus.CLOSED, service.get_event(event_id).status)

    def test_exchange_events_stay_contract_valid_with_incrementing_versions(self) -> None:
        service = self.make_service()
        event_id = self.locked_event(service)
        task = service.assign_transport(
            event_id, ambulance_id="A1", hospital_id="H1", occurred_at=T3, actor=SUPERVISOR
        )
        service.hospital_accept(task.task_id, occurred_at=T4, actor=HOSPITAL)
        service.record_handoff(task.task_id, note="交接完成", occurred_at=T5, actor=MEDIC)
        for payload in service.exchange_events:
            self.assertEqual([], validate_event(payload, self.schema))
        by_aggregate: dict[str, list[int]] = {}
        for payload in service.exchange_events:
            by_aggregate.setdefault(payload["aggregate_id"], []).append(payload["version"])
        for versions in by_aggregate.values():
            self.assertEqual(list(range(1, len(versions) + 1)), versions)


if __name__ == "__main__":
    unittest.main()
