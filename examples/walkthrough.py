"""端到端联调走查：一条可疑卒中来电的完整协同过程。

运行：

    python3 examples/walkthrough.py

会把全过程事件打印为 JSON；data/sample_flow.json 即由本脚本的
``build_flow`` 生成，测试会校验文件与脚本一致且每条事件满足契约。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stroke_emergency_coordination.review import render_timeline
from stroke_emergency_coordination.service import CoordinationService

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 24, 11, 58, tzinfo=CST)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def build_flow() -> tuple[CoordinationService, list[str]]:
    service = CoordinationService()
    case_id = "case-20260924-001"

    # 接诊能力建册：H1 综合卒中中心较远，H2 初级卒中中心更近
    service.register_hospital(
        hospital_id="H1", name="市第一医院（综合卒中中心）",
        capability="comprehensive", beds_available=2, eta_minutes=25, at=at(0),
    )
    service.register_hospital(
        hospital_id="H2", name="区人民医院（卒中防治中心）",
        capability="primary", beds_available=1, eta_minutes=12, at=at(0),
    )
    service.register_hospital(
        hospital_id="H3", name="城北医院",
        capability="basic", beds_available=0, status="full", eta_minutes=9,
        at=at(0), reason="临时满负荷，暂不接诊卒中",
    )

    # 12:00 接线：登记来电与定位授权
    service.register_call(
        case_id=case_id, caller_number="13800001111", device_id="dev-watch-007",
        operator_id="dispatcher-li", at=at(2),
        address="幸福小区 3 号楼 502", location_authorized=True,
    )

    # 12:01 登记症状原话与发病时间：命中红色阈值 → 锁定、指导、自动转运选择
    service.add_report(
        case_id=case_id, kind="symptom",
        text="我老伴突然剧烈头痛，吐了两次，现在说话含糊，右边手脚抬不起来",
        source="家属（配偶）", operator_id="dispatcher-li",
        onset_time=datetime(2026, 9, 24, 11, 40, tzinfo=CST), at=at(3),
    )
    # 既往风险与用药（补充上下文，不降低已触发级别）
    service.add_report(
        case_id=case_id, kind="history", text="既往有脑梗、高血压",
        source="家属（配偶）", operator_id="dispatcher-li", at=at(4),
    )
    service.add_report(
        case_id=case_id, kind="medication", text="长期服用阿司匹林",
        source="家属（配偶）", operator_id="dispatcher-li", at=at(4),
    )

    # 家属确认收到“保持气道通畅、禁食水药、勿随意搬动”的指导
    service.acknowledge_family(case_id=case_id, family_member="配偶王女士", at=at(5))

    # 12:06 H1 临时满负荷：任务尚未出发，自动改派更近的 H2
    service.update_capacity(
        hospital_id="H1", beds_available=0, status="full",
        reason="急诊抢救室满负荷", at=at(8),
    )

    # 派车、出发
    service.assign_ambulance(case_id=case_id, ambulance_id="A-12", at=at(9), operator_id="fleet-zhao")
    service.depart(case_id=case_id, at=at(10), operator_id="A-12-carer")

    # 途中失联：状态本地继续推进，恢复后只补传未确认回执
    service.ambulance_offline(case_id=case_id, at=at(12), note="进入隧道区间")

    # 12:18 H2 也告满：但 A-12 已出发，路线冻结，不再重算
    service.update_capacity(
        hospital_id="H2", beds_available=0, status="full",
        reason="CT 室临时故障", at=at(20),
    )

    # 失联期间急救车仍到达现场：状态只在车载本地推进，中心尚不可见
    local_arrivals = service.offline_arrive(case_id=case_id, at=at(26), phase="scene")

    # 12:28 H2 恢复可接诊
    service.update_capacity(
        hospital_id="H2", beds_available=1, status="open",
        reason="CT 室恢复", at=at(30),
    )
    # 恢复连接：仅补传本地未确认回执的事件（到场），重传幂等
    reconnect_events = service.ambulance_reconnect(case_id=case_id, at=at(32))
    synced_ids = reconnect_events[0].payload["synced_event_ids"]
    assert len(synced_ids) == len(local_arrivals) == 1, "恢复后应只补传未确认的本地产物"
    # 失联期间中心侧看不到到场，恢复后才可见
    assert synced_ids[0] == local_arrivals[0].event_id
    # 再次补传同一批事件必须幂等，不产生重复事实
    assert service.sync_ambulance_events(local_arrivals) == ()
    service.hospital_accept(case_id=case_id, hospital_id="H2", at=at(33), operator_id="H2-stroke-nurse")
    service.arrive(case_id=case_id, at=at(44), phase="hospital", operator_id="A-12-carer")
    service.record_handoff(
        case_id=case_id, at=at(46), from_staff="A-12-急救员周师傅",
        to_staff="H2-卒中绿道护士", note="发病时间 11:40，院前已禁食水药",
        vital_signs={"血压": "188/102", "心率": 96, "血氧": "95%", "意识": "嗜睡"},
    )
    return service, [case_id]


def all_event_dicts(service: CoordinationService) -> list[dict]:
    return [event.to_dict() for event in service.store.all_events()]


def main() -> None:
    service, case_ids = build_flow()
    print(json.dumps(all_event_dicts(service), ensure_ascii=False, indent=2))
    print("\n=== 复盘时间线 ===", file=sys.stderr)
    for case_id in case_ids:
        print(render_timeline(service.case_timeline_events(case_id)), file=sys.stderr)


if __name__ == "__main__":
    main()
