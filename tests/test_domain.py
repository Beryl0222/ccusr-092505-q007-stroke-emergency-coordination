from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stroke_emergency_coordination.aggregate import EmergencyCase
from stroke_emergency_coordination.contracts import validate_event
from stroke_emergency_coordination.errors import DomainError
from stroke_emergency_coordination.event import DomainEvent
from stroke_emergency_coordination.offline import AmbulanceLocalLog
from stroke_emergency_coordination.review import build_timeline
from stroke_emergency_coordination.service import CoordinationService
from stroke_emergency_coordination.triage import (
    TriageLevel,
    evaluate_triage,
)
from stroke_emergency_coordination.transport import Hospital, rank_hospitals

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 24, 12, 0, tzinfo=CST)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


class TriageRuleTests(unittest.TestCase):
    def test_red_signal_locks_level_and_carries_evidence(self) -> None:
        result = evaluate_triage(
            symptom_quotes=["突然剧烈头痛，吐了两次", "说话含糊，右边手脚抬不起来"],
            risk_history=["既往脑梗"],
            medications=["阿司匹林"],
        )
        self.assertEqual(result.level, TriageLevel.RED)
        self.assertFalse(result.is_diagnosis)
        self.assertIn("不是诊断结论", result.hint_text)
        codes = {rule.code for rule in result.matched}
        self.assertIn("RED_SYMPTOM", codes)
        self.assertTrue(all(rule.evidence for rule in result.matched))

    def test_headache_with_vomiting_is_orange_without_red_words(self) -> None:
        result = evaluate_triage(["头痛", "吐了"])
        self.assertEqual(result.level, TriageLevel.ORANGE)

    def test_benign_quote_is_green(self) -> None:
        self.assertEqual(evaluate_triage(["有点累，想睡觉"]).level, TriageLevel.GREEN)

    def test_prior_stroke_plus_neuro_symptom_and_anticoagulant_escalates(self) -> None:
        result = evaluate_triage(["头痛，吐了"], risk_history=["既往有中风"], medications=["华法林"])
        self.assertEqual(result.level, TriageLevel.RED)


class LockAndMonotonicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = CoordinationService()
        self.case_id = "case-1"
        self.service.register_hospital(
            hospital_id="H1", name="综合卒中中心", capability="comprehensive",
            beds_available=2, eta_minutes=20, at=at(0),
        )
        self.service.register_call(
            case_id=self.case_id, caller_number="13800001111", device_id=None,
            operator_id="op-1", at=at(0),
        )

    def test_red_threshold_locks_and_emits_guidance_including_three_taboos(self) -> None:
        events = self.service.add_report(
            case_id=self.case_id, kind="symptom",
            text="突发剧烈头痛、呕吐、言语含糊", source="家属",
            operator_id="op-1", at=at(1),
        )
        kinds = [event.event_type for event in events]
        self.assertIn("RISK_LOCKED", kinds)
        self.assertIn("ON_SCENE_GUIDANCE_ISSUED", kinds)
        view = self.service.view_case(self.case_id, "dispatcher")
        self.assertTrue(view.locked)
        joined = " ".join(view.guidance)
        self.assertIn("呼吸道", joined)
        self.assertIn("不要给患者喂水", joined)
        self.assertIn("不要随意搬动", joined)

    def test_later_information_can_only_add_context_never_lower_level(self) -> None:
        self.service.add_report(
            case_id=self.case_id, kind="symptom", text="剧烈头痛伴说话不清",
            source="家属", operator_id="op-1", at=at(1),
        )
        lock_count_before = sum(
            e.event_type == "RISK_LOCKED" for e in self.service.case_events(self.case_id)
        )
        # 后续补充无高危词的资料（甚至专业人员给出更低分级），级别不得下降
        self.service.add_report(
            case_id=self.case_id, kind="symptom", text="家属补充：患者刚才有点头晕",
            source="家属", operator_id="op-1", at=at(2),
        )
        self.service.professional_decision(
            case_id=self.case_id, professional_id="doc-9", role="medical",
            level=TriageLevel.YELLOW.value, assessment="建议留观", at=at(3),
        )
        case = self.service.view_case(self.case_id, "medical")
        self.assertEqual(case.level_label, TriageLevel.RED.label)
        self.assertTrue(case.locked)
        lock_count_after = sum(
            e.event_type == "RISK_LOCKED" for e in self.service.case_events(self.case_id)
        )
        self.assertEqual(lock_count_before, lock_count_after)

    def test_professional_red_can_lock_and_later_hint_cannot_downgrade(self) -> None:
        # 症状本身未命中高危词，专业人员判断为红色 → 锁定
        self.service.add_report(
            case_id=self.case_id, kind="symptom", text="有点头晕，精神差",
            source="家属", operator_id="op-1", at=at(1),
        )
        self.service.professional_decision(
            case_id=self.case_id, professional_id="doc-1", role="medical",
            level=TriageLevel.RED.value, assessment="高度怀疑后循环卒中", at=at(2),
        )
        self.assertTrue(self.service.view_case(self.case_id, "medical").locked)
        # 再次登记低危资料：自动重评为黄色（仅“头晕”），但级别字段维持红色并给出说明
        self.service.add_report(
            case_id=self.case_id, kind="symptom", text="家属又说可能只是没休息好，头有点晕",
            source="家属", operator_id="op-1", at=at(3),
        )
        last_hint = [
            e for e in self.service.case_events(self.case_id)
            if e.event_type == "TRIAGE_HINT_EMITTED"
        ][-1]
        self.assertEqual(last_hint.payload["raw_level"], TriageLevel.YELLOW.value)
        self.assertEqual(last_hint.payload["level"], TriageLevel.RED.value)
        self.assertIn("只升不降", last_hint.payload["hint_text"])

    def test_system_hint_is_never_marked_as_diagnosis(self) -> None:
        self.service.add_report(
            case_id=self.case_id, kind="symptom", text="剧烈头痛、口角歪斜",
            source="家属", operator_id="op-1", at=at(1),
        )
        hints = [
            event for event in self.service.case_events(self.case_id)
            if event.event_type == "TRIAGE_HINT_EMITTED"
        ]
        self.assertTrue(hints)
        for hint in hints:
            self.assertFalse(hint.payload["is_diagnosis"])
        decisions = [
            event for event in self.service.case_events(self.case_id)
            if event.event_type == "PROFESSIONAL_DECISION_RECORDED"
        ]
        self.assertEqual([], decisions)  # 系统不会替专业人员生成判断


class OnsetTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = CoordinationService()
        self.service.register_call(
            case_id="case-t", caller_number="13800002222", device_id=None,
            operator_id="op-1", at=at(0),
        )

    def _conflicting_reports(self) -> None:
        self.service.add_report(
            case_id="case-t", kind="symptom", text="家属补充发病时间（无其他症状）",
            source="配偶", operator_id="op-1",
            onset_time=datetime(2026, 9, 24, 11, 40, tzinfo=CST), at=at(1),
        )
        self.service.add_report(
            case_id="case-t", kind="symptom", text="女儿回忆发病更早（无其他症状）",
            source="女儿", operator_id="op-1",
            onset_time=datetime(2026, 9, 24, 11, 20, tzinfo=CST), at=at(2),
        )

    def test_conflicting_onset_times_are_kept_and_flagged(self) -> None:
        self._conflicting_reports()
        case = EmergencyCase.replay(
            "case-t", self.service.case_events("case-t"), lambda kind: "x"
        )
        self.assertTrue(case.onset_conflict)
        reported = {report.value for report in case.onset_reports}
        self.assertEqual(
            {datetime(2026, 9, 24, 11, 40, tzinfo=CST),
             datetime(2026, 9, 24, 11, 20, tzinfo=CST)},
            reported,
        )
        # 冲突未更正前，权威值仍是首次登记值，不被悄悄覆盖
        self.assertEqual(case.onset_authoritative, datetime(2026, 9, 24, 11, 40, tzinfo=CST))

    def test_only_designated_role_may_correct_and_original_is_preserved(self) -> None:
        self._conflicting_reports()
        with self.assertRaises(DomainError) as blocked:
            self.service.correct_onset_time(
                case_id="case-t", operator_id="op-1", operator_role="dispatcher",
                new_value=datetime(2026, 9, 24, 11, 20, tzinfo=CST),
                reason="监控核实为 11:20", at=at(3),
            )
        self.assertEqual(blocked.exception.code, "forbidden_time_correction")
        self.service.correct_onset_time(
            case_id="case-t", operator_id="sup-1", operator_role="supervisor",
            new_value=datetime(2026, 9, 24, 11, 20, tzinfo=CST),
            reason="调取楼道监控核实", at=at(4),
        )
        correction = [
            event for event in self.service.case_events("case-t")
            if event.event_type == "ONSET_TIME_CORRECTED"
        ][-1]
        self.assertEqual(
            correction.payload["previous_value"], "2026-09-24T11:40:00+08:00"
        )
        self.assertEqual(len(correction.payload["original_reports"]), 2)


class DuplicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = CoordinationService()

    def test_same_caller_new_case_id_is_merged(self) -> None:
        self.service.register_call(
            case_id="case-a", caller_number="13800003333", device_id=None,
            operator_id="op-1", at=at(0),
        )
        case, events = self.service.register_call(
            case_id="case-b", caller_number="13800003333", device_id=None,
            operator_id="op-2", at=at(1),
        )
        self.assertEqual(case.case_id, "case-a")
        self.assertIn("DUPLICATE_REPORT_MERGED", [e.event_type for e in events])

    def test_same_device_replay_is_rejected_idempotently(self) -> None:
        self.service.register_call(
            case_id="case-a", caller_number="13800004444", device_id="dev-1",
            operator_id="op-1", at=at(0),
        )
        case, events = self.service.register_call(
            case_id="case-a", caller_number="13800004444", device_id="dev-1",
            operator_id="op-1", at=at(1),
        )
        self.assertEqual(case.case_id, "case-a")
        self.assertIn("DUPLICATE_REPORT_REJECTED", [e.event_type for e in events])


def _service_with_hospitals() -> CoordinationService:
    service = CoordinationService()
    service.register_hospital(
        hospital_id="HC", name="综合中心（远）", capability="comprehensive",
        beds_available=2, eta_minutes=25, at=at(0),
    )
    service.register_hospital(
        hospital_id="HP", name="初级中心（近）", capability="primary",
        beds_available=2, eta_minutes=10, at=at(0),
    )
    return service


class RoutingTests(unittest.TestCase):
    def test_capability_outranks_eta(self) -> None:
        ranked = rank_hospitals([
            Hospital("HP", "近", "primary", eta_minutes=10, beds_available=1),
            Hospital("HC", "远", "comprehensive", eta_minutes=25, beds_available=1),
        ])
        self.assertEqual("HC", ranked[0].hospital_id)

    def test_full_hospital_excluded_from_candidates(self) -> None:
        hospitals = [
            Hospital("HC", "综合", "comprehensive", eta_minutes=25, beds_available=0, status="full"),
            Hospital("HP", "初级", "primary", eta_minutes=10, beds_available=1),
        ]
        self.assertEqual("HP", rank_hospitals(hospitals)[0].hospital_id)

    def test_capacity_change_recalculates_only_undeparted_routes(self) -> None:
        service = _service_with_hospitals()
        service.register_call(
            case_id="case-r", caller_number="13800005555", device_id=None,
            operator_id="op-1", at=at(0),
        )
        service.add_report(
            case_id="case-r", kind="symptom", text="剧烈头痛伴言语不清",
            source="家属", operator_id="op-1", at=at(1),
        )
        task_id = "transport-case-r"
        chosen_before = service.store.load(task_id)[-1].payload["chosen_hospital_id"]
        self.assertEqual("HC", chosen_before)

        # HC 满负荷，任务未出发：自动改派 HP
        service.update_capacity(
            hospital_id="HC", beds_available=0, status="full",
            reason="抢救室满", at=at(3),
        )
        service.assign_ambulance(
            case_id="case-r", ambulance_id="A-1", at=at(4), operator_id="fleet"
        )
        service.depart(case_id="case-r", at=at(5), operator_id="A-1")
        task_events_after_departure = len(service.store.load(task_id))

        # HP 再满：已出发，路线冻结，不得重算
        service.update_capacity(
            hospital_id="HP", beds_available=0, status="full",
            reason="CT 故障", at=at(8),
        )
        self.assertEqual(task_events_after_departure, len(service.store.load(task_id)))
        task = service._load_task(task_id)  # noqa: SLF001 - 测试直接读状态
        self.assertEqual("HP", task.hospital_id)
        self.assertTrue(task.route_frozen)
        with self.assertRaises(DomainError) as blocked:
            task.propose_routes(
                hospitals=[], at=at(9), operator_id="x",
                trigger="manual", recalculated=True,
            )
        self.assertEqual(blocked.exception.code, "route_frozen")

    def test_transport_state_machine_rejects_bad_transitions(self) -> None:
        service = _service_with_hospitals()
        service.register_call(
            case_id="case-s", caller_number="13800006666", device_id=None,
            operator_id="op-1", at=at(0),
        )
        service.add_report(
            case_id="case-s", kind="symptom", text="一侧肢体无力、剧烈头痛",
            source="家属", operator_id="op-1", at=at(1),
        )
        # 未派车不能出发
        with self.assertRaises(DomainError):
            service.depart(case_id="case-s", at=at(2), operator_id="A-2")

    def test_lock_without_available_hospital_suspends_then_opens_on_recovery(self) -> None:
        service = CoordinationService()
        # 唯一医院建册即满负荷
        service.register_hospital(
            hospital_id="HF", name="已满医院", capability="primary",
            beds_available=0, status="full", eta_minutes=12, at=at(0),
        )
        service.register_call(
            case_id="case-w", caller_number="13800001234", device_id=None,
            operator_id="op-1", at=at(0),
        )
        service.add_report(
            case_id="case-w", kind="symptom", text="剧烈头痛伴言语不清",
            source="家属", operator_id="op-1", at=at(1),
        )
        task_id = "transport-case-w"
        self.assertIn("ROUTE_SUSPENDED", [e.event_type for e in service.store.load(task_id)])
        self.assertIsNone(service._load_task(task_id).hospital_id)  # noqa: SLF001
        # 尚未开线，派车被拒绝
        with self.assertRaises(DomainError) as blocked:
            service.assign_ambulance(case_id="case-w", ambulance_id="A-5", at=at(2), operator_id="fleet")
        self.assertEqual(blocked.exception.code, "no_route")
        # 医院恢复容量：挂起任务自动开线
        service.update_capacity(
            hospital_id="HF", beds_available=1, status="open",
            reason="腾出抢救位", at=at(5),
        )
        task = service._load_task(task_id)  # noqa: SLF001
        self.assertEqual("HF", task.hospital_id)
        self.assertEqual("proposed", task.status)
        self.assertIn(
            "ROUTE_PROPOSED",
            [e.event_type for e in service.store.load(task_id)],
        )


class OfflineTests(unittest.TestCase):
    def _locked_service(self) -> tuple[CoordinationService, str]:
        service = _service_with_hospitals()
        service.register_call(
            case_id="case-o", caller_number="13800007777", device_id=None,
            operator_id="op-1", at=at(0),
        )
        service.add_report(
            case_id="case-o", kind="symptom", text="剧烈头痛、说话不清",
            source="家属", operator_id="op-1", at=at(1),
        )
        service.assign_ambulance(
            case_id="case-o", ambulance_id="A-9", at=at(2), operator_id="fleet"
        )
        service.depart(case_id="case-o", at=at(3), operator_id="A-9")
        return service, "case-o"

    def test_local_progress_is_invisible_until_reconnect_and_sync_is_idempotent(self) -> None:
        service, case_id = self._locked_service()
        service.ambulance_offline(case_id=case_id, at=at(4))
        local = service.offline_arrive(case_id=case_id, at=at(6), phase="scene")
        task_id = "transport-case-o"
        central_types = [e.event_type for e in service.store.load(task_id)]
        self.assertNotIn("AMBULANCE_ARRIVED", central_types)  # 失联期间中心不可见

        reconnect = service.ambulance_reconnect(case_id=case_id, at=at(8))
        self.assertEqual(reconnect[0].payload["synced_event_ids"], [local[0].event_id])
        self.assertIn(
            "AMBULANCE_ARRIVED", [e.event_type for e in service.store.load(task_id)]
        )
        # 重复补传幂等：不新增事实
        self.assertEqual((), service.sync_ambulance_events(local))

    def test_local_log_only_resends_unacknowledged(self) -> None:
        log = AmbulanceLocalLog("A-9", lambda kind: f"e-{kind}")
        event = DomainEvent(
            event_id="e-1", event_type="AMBULANCE_ARRIVED", aggregate_type="transport_task",
            aggregate_id="t-1", occurred_at=at(0), version=1, summary="到场",
        )
        log.record_local(event)
        log.mark_acked("e-1")
        self.assertEqual((), log.pending_sync())


class AccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = CoordinationService()
        self.service.register_call(
            case_id="case-acl", caller_number="13800008888", device_id=None,
            operator_id="op-1", at=at(0), address="保密小区 1 号",
            location_authorized=True,
        )
        self.service.add_report(
            case_id="case-acl", kind="symptom", text="剧烈头痛伴呕吐",
            source="家属", operator_id="op-1", at=at(1),
        )
        self.service.add_report(
            case_id="case-acl", kind="history", text="既往脑出血、高血压",
            source="家属", operator_id="op-1", at=at(2),
        )
        self.service.add_report(
            case_id="case-acl", kind="medication", text="利伐沙班",
            source="家属", operator_id="op-1", at=at(2),
        )

    def test_location_and_history_follow_role_minimum_visibility(self) -> None:
        dispatcher = self.service.view_case("case-acl", "dispatcher")
        self.assertEqual("保密小区 1 号", dispatcher.address)
        self.assertEqual((), dispatcher.history)  # 接线员不看病史
        self.assertEqual((), dispatcher.medications)

        viewer = self.service.view_case("case-acl", "viewer")
        self.assertIsNone(viewer.address)
        self.assertEqual((), viewer.history)
        self.assertTrue(any("位置" in note for note in viewer.redaction_notes))

        medical = self.service.view_case("case-acl", "medical")
        self.assertEqual("保密小区 1 号", medical.address)
        self.assertIn("既往脑出血、高血压", medical.history)
        self.assertIn("利伐沙班", medical.medications)

    def test_revoked_authorization_hides_location_from_everyone(self) -> None:
        self.service.revoke_location(case_id="case-acl", operator_id="op-1", at=at(3))
        for role in ("dispatcher", "medical", "supervisor"):
            self.assertIsNone(self.service.view_case("case-acl", role).address)


class ReviewTests(unittest.TestCase):
    def test_timeline_reconstructs_judgments_notifications_and_handoff(self) -> None:
        service = _service_with_hospitals()
        service.register_call(
            case_id="case-rev", caller_number="13800009999", device_id=None,
            operator_id="op-1", at=at(0),
        )
        service.add_report(
            case_id="case-rev", kind="symptom", text="剧烈头痛、言语含糊",
            source="家属", operator_id="op-1", at=at(1),
        )
        service.acknowledge_family(case_id="case-rev", family_member="配偶", at=at(2))
        service.assign_ambulance(
            case_id="case-rev", ambulance_id="A-3", at=at(3), operator_id="fleet"
        )
        service.depart(case_id="case-rev", at=at(4), operator_id="A-3")
        service.arrive(case_id="case-rev", at=at(10), phase="scene", operator_id="A-3")
        service.hospital_accept(
            case_id="case-rev", hospital_id="HC", at=at(11), operator_id="HC-nurse"
        )
        service.arrive(case_id="case-rev", at=at(30), phase="hospital", operator_id="A-3")
        service.record_handoff(
            case_id="case-rev", at=at(31), from_staff="A-3", to_staff="HC-green",
            vital_signs={"血氧": "96%"},
        )
        entries = build_timeline(service.case_timeline_events("case-rev"))
        categories = {entry.category for entry in entries}
        self.assertIn("judgment", categories)
        self.assertIn("notification", categories)
        self.assertIn("handoff", categories)

        hints = [entry for entry in entries if entry.event_type == "TRIAGE_HINT_EMITTED"]
        self.assertTrue(hints)
        for entry in hints:
            self.assertEqual("system_hint", entry.source)
            self.assertFalse(entry.detail["is_diagnosis"])
        handoff = [entry for entry in entries if entry.category == "handoff"][-1]
        self.assertEqual("2026-09-24T12:31:00+08:00", handoff.at)


class ContractFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))

    def test_single_sample_still_valid(self) -> None:
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual([], validate_event(sample, self.schema))

    def test_every_event_in_sample_flow_satisfies_contract(self) -> None:
        flow = json.loads((ROOT / "data" / "sample_flow.json").read_text(encoding="utf-8"))
        self.assertGreater(len(flow), 15)
        for event in flow:
            self.assertEqual([], validate_event(event, self.schema), event["event_type"])


class StoreTests(unittest.TestCase):
    def test_duplicate_event_id_and_version_gap_are_rejected(self) -> None:
        service = CoordinationService()
        service.register_call(
            case_id="case-x", caller_number="13800000000", device_id=None,
            operator_id="op-1", at=at(0),
        )
        events = service.store.load("case-x")
        with self.assertRaises(DomainError) as dup:
            service.store.append(events[0])
        self.assertEqual(dup.exception.code, "event_id_duplicated")
        forged = DomainEvent(
            event_id="forged", event_type="SYMPTOM_REPORTED", aggregate_type="emergency_call",
            aggregate_id="case-x", occurred_at=at(1), version=99, summary="伪造版本",
        )
        with self.assertRaises(DomainError) as gap:
            service.store.append(forged)
        self.assertEqual(gap.exception.code, "version_conflict")


if __name__ == "__main__":
    unittest.main()
