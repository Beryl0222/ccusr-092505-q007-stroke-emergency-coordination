"""急症预警转运协同的领域模型。

只依赖标准库。模型层不引入业务流程，流程规则见 service.py。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    """系统内可识别的操作角色。"""

    SYSTEM = "system"  # 自动提示引擎，不是专业人员
    DISPATCHER = "dispatcher"  # 接线员
    SUPERVISOR = "supervisor"  # 调度负责人，发病时间冲突的指定更正人
    MEDIC = "medic"  # 急救车随车人员
    HOSPITAL_STAFF = "hospital_staff"  # 接诊医院人员
    AUDITOR = "auditor"  # 事后复盘人员


class TriageLevel(str, Enum):
    ROUTINE = "routine"
    URGENT = "urgent"
    CRITICAL = "critical"


LEVEL_RANK = {
    TriageLevel.ROUTINE: 0,
    TriageLevel.URGENT: 1,
    TriageLevel.CRITICAL: 2,
}


def higher_level(left: TriageLevel | None, right: TriageLevel | None) -> TriageLevel | None:
    """取两者中更高的级别；任一为 None 时返回另一个。"""
    if left is None:
        return right
    if right is None:
        return left
    return left if LEVEL_RANK[left] >= LEVEL_RANK[right] else right


class AssessmentSource(str, Enum):
    """评估来源：自动提示与专业人员判断必须分开记录。"""

    SYSTEM_HINT = "system_hint"
    PROFESSIONAL = "professional"


SYSTEM_HINT_DISCLAIMER = "系统自动提示，非诊断结论"


class EventStatus(str, Enum):
    OPEN = "open"
    LOCKED = "locked"  # 高危信号达到阈值后锁定
    CLOSED = "closed"  # 交接完成后关闭


class TransportPhase(str, Enum):
    SELECTING = "selecting"  # 转运选择中
    ASSIGNED = "assigned"  # 已指派急救车与目标医院
    DEPARTED = "departed"  # 急救车已出发
    ARRIVED = "arrived"  # 已到达
    HANDED_OFF = "handed_off"  # 已完成交接


class AmbulanceStatus(str, Enum):
    AVAILABLE = "available"
    DISPATCHED = "dispatched"
    OFFLINE = "offline"  # 失联，本地状态继续推进


# 锁定后下发给现场的标准禁忌指导语。
STANDARD_GUIDANCE: tuple[str, ...] = (
    "保持呼吸道通畅",
    "避免口服饮水或药物",
    "避免随意搬动患者",
)


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: Role


SYSTEM_ACTOR = Actor("system", Role.SYSTEM)


@dataclass(frozen=True)
class SymptomEntry:
    verbatim: str  # 症状原话，不做改写
    recorded_at: str
    recorded_by: str


@dataclass(frozen=True)
class OnsetCorrection:
    """发病时间更正记录，原值必须保留。"""

    original: str | None
    corrected: str
    corrected_by: str
    occurred_at: str
    reason: str


@dataclass(frozen=True)
class Medication:
    name: str
    dose: str
    note: str
    recorded_at: str


@dataclass(frozen=True)
class FamilyConfirmation:
    confirmer: str
    content: str
    occurred_at: str


@dataclass(frozen=True)
class Assessment:
    source: AssessmentSource
    level: TriageLevel
    signals: tuple[str, ...]
    actor_id: str
    occurred_at: str
    note: str = ""


@dataclass(frozen=True)
class GuidanceInstruction:
    text: str
    issued_at: str


@dataclass(frozen=True)
class Supplement:
    """补充资料：只增加上下文，不降低已触发的级别。"""

    note: str
    actor_id: str
    occurred_at: str
    suggested_level: TriageLevel | None = None


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    occurred_at: str
    kind: str
    actor_id: str
    detail: dict


@dataclass
class EmergencyEvent:
    event_id: str
    caller_id: str
    device_id: str | None
    status: EventStatus
    created_at: str
    last_activity_at: str
    symptoms: list[SymptomEntry] = field(default_factory=list)
    onset_at: str | None = None
    onset_corrections: list[OnsetCorrection] = field(default_factory=list)
    risk_history: list[str] = field(default_factory=list)
    medications: list[Medication] = field(default_factory=list)
    location_consent: bool = False
    location: str | None = None
    family_confirmations: list[FamilyConfirmation] = field(default_factory=list)
    assessments: list[Assessment] = field(default_factory=list)
    system_level: TriageLevel | None = None
    professional_level: TriageLevel | None = None
    context_level: TriageLevel | None = None
    locked_level: TriageLevel | None = None
    locked_at: str | None = None
    guidance: list[GuidanceInstruction] = field(default_factory=list)
    supplements: list[Supplement] = field(default_factory=list)
    transport_task_id: str | None = None
    audit: list[AuditEntry] = field(default_factory=list)

    @property
    def effective_level(self) -> TriageLevel:
        """综合级别：各通道与锁定底线取最高，已触发级别不可被拉低。"""
        level: TriageLevel | None = self.locked_level
        for candidate in (self.system_level, self.professional_level, self.context_level):
            level = higher_level(level, candidate)
        return level if level is not None else TriageLevel.ROUTINE


@dataclass(frozen=True)
class Receipt:
    """本地推进产生的待确认回执。"""

    receipt_id: str
    phase: TransportPhase
    note: str
    occurred_at: str
    acked: bool = False


@dataclass
class TransportTask:
    task_id: str
    event_id: str
    phase: TransportPhase
    candidate_hospitals: list[str] = field(default_factory=list)
    ambulance_id: str | None = None
    hospital_id: str | None = None
    receipts: list[Receipt] = field(default_factory=list)

    @property
    def departed(self) -> bool:
        return self.phase in (
            TransportPhase.DEPARTED,
            TransportPhase.ARRIVED,
            TransportPhase.HANDED_OFF,
        )

    def pending_receipts(self) -> list[Receipt]:
        return [receipt for receipt in self.receipts if not receipt.acked]


@dataclass
class HospitalCapacity:
    hospital_id: str
    name: str
    accepting: bool = True
    note: str = ""
    updated_at: str = ""


@dataclass
class AmbulanceState:
    ambulance_id: str
    status: AmbulanceStatus
    updated_at: str


@dataclass(frozen=True)
class TriageRule:
    """已登记的分诊规则：命中关键词达到阈值即产生系统提示。"""

    rule_id: str
    description: str
    keywords: tuple[str, ...]
    threshold: int


# 角色最小可见字段。位置与病史按角色裁剪，未授权定位时位置一律不可见。
_ALL_FIELDS = frozenset(
    {
        "event_id",
        "status",
        "triage_level",
        "symptoms",
        "onset_at",
        "location",
        "medical_history",
        "medications",
        "guidance",
        "family_confirmations",
        "transport",
        "audit",
    }
)

ROLE_VISIBLE_FIELDS: dict[Role, frozenset[str]] = {
    Role.SUPERVISOR: _ALL_FIELDS,
    Role.DISPATCHER: frozenset(
        {
            "event_id",
            "status",
            "triage_level",
            "symptoms",
            "onset_at",
            "location",
            "guidance",
            "family_confirmations",
            "transport",
        }
    ),
    Role.MEDIC: frozenset(
        {
            "event_id",
            "status",
            "triage_level",
            "symptoms",
            "onset_at",
            "location",
            "medical_history",
            "medications",
            "guidance",
            "transport",
        }
    ),
    Role.HOSPITAL_STAFF: frozenset(
        {
            "event_id",
            "status",
            "triage_level",
            "symptoms",
            "onset_at",
            "medical_history",
            "medications",
            "transport",
        }
    ),
    Role.AUDITOR: frozenset({"event_id", "status", "triage_level", "audit"}),
    Role.SYSTEM: frozenset({"event_id", "status", "triage_level"}),
}
