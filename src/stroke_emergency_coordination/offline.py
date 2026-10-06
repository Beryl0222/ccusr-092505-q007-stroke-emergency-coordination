"""急救车失联期间的本地推进与恢复补传。

- 失联时车载端继续把状态变更写入本地日志（LOCAL），业务状态机照常推进；
- 恢复连接后只上传"尚未拿到确认回执"的事件；
- 服务端按事件 event_id 幂等去重，已确认的事件重传不会产生重复事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from .errors import DomainError
from .event import DomainEvent

# 本地事件的确认状态
PENDING = "pending"
ACKED = "acked"
SYNCED = "synced"


@dataclass
class LocalEntry:
    event: DomainEvent
    receipt: str = PENDING  # pending / acked / synced


class AmbulanceLocalLog:
    """车载本地日志：离线可写，恢复后只挑未确认的条目补传。"""

    def __init__(self, ambulance_id: str, next_id: Callable[[str], str]) -> None:
        self.ambulance_id = ambulance_id
        self._next_id = next_id
        self.entries: list[LocalEntry] = []

    def record_local(self, event: DomainEvent) -> LocalEntry:
        if any(entry.event.event_id == event.event_id for entry in self.entries):
            raise DomainError("event_id_duplicated", f"本地日志已存在 {event.event_id}")
        entry = LocalEntry(event=event)
        self.entries.append(entry)
        return entry

    def mark_acked(self, event_id: str) -> None:
        for entry in self.entries:
            if entry.event.event_id == event_id:
                entry.receipt = ACKED

    def pending_sync(self) -> tuple[DomainEvent, ...]:
        """恢复连接时调用：只返回没有确认回执的事件。"""
        return tuple(entry.event for entry in self.entries if entry.receipt == PENDING)

    def mark_all_synced(self, event_ids: Sequence[str]) -> None:
        wanted = set(event_ids)
        for entry in self.entries:
            if entry.event.event_id in wanted:
                entry.receipt = SYNCED
