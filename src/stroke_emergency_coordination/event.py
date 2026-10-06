"""领域事件信封。

与 contracts/domain.schema.json 对齐：统一聚合标识、事件版本、
带时区的发生时间；业务数据放在 payload 中。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return value


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ensure_aware(parsed)


@dataclass(frozen=True)
class DomainEvent:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime
    version: int
    summary: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None
    causation_id: str | None = None
    operator_id: str | None = None
    role_required: str | None = None

    def to_dict(self) -> dict[str, Any]:
        ensure_aware(self.occurred_at)
        data: dict[str, Any] = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at.isoformat(),
            "version": self.version,
            "summary": self.summary,
            "payload": dict(self.payload),
        }
        if self.correlation_id is not None:
            data["correlation_id"] = self.correlation_id
        if self.causation_id is not None:
            data["causation_id"] = self.causation_id
        if self.operator_id is not None:
            data["operator_id"] = self.operator_id
        if self.role_required is not None:
            data["role_required"] = self.role_required
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DomainEvent":
        return cls(
            event_id=data["event_id"],
            event_type=data["event_type"],
            aggregate_type=data["aggregate_type"],
            aggregate_id=data["aggregate_id"],
            occurred_at=parse_time(data["occurred_at"]),
            version=int(data["version"]),
            summary=data["summary"],
            payload=dict(data.get("payload", {})),
            correlation_id=data.get("correlation_id"),
            causation_id=data.get("causation_id"),
            operator_id=data.get("operator_id"),
            role_required=data.get("role_required"),
        )
