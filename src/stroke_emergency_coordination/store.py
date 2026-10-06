"""内存事件存储。

按聚合保存不可变事件流，校验版本连续、event_id 全局唯一，
并提供幂等键索引：同一客户端请求重放时直接返回已产生的事件，
不会重复追加（急救车离线补传依赖这一点）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .errors import DomainError
from .event import DomainEvent


class EventStore:
    def __init__(self) -> None:
        self._streams: dict[str, list[DomainEvent]] = defaultdict(list)
        self._event_ids: set[str] = set()
        self._idempotency: dict[tuple[str, str], tuple[DomainEvent, ...]] = {}
        self._commit_order: list[DomainEvent] = []

    def append(self, *events: DomainEvent) -> tuple[DomainEvent, ...]:
        for event in events:
            if event.event_id in self._event_ids:
                raise DomainError("event_id_duplicated", f"事件标识重复：{event.event_id}")
        committed: list[DomainEvent] = []
        for event in events:
            stream = self._streams[event.aggregate_id]
            expected = len(stream) + 1
            if event.version != expected:
                raise DomainError(
                    "version_conflict",
                    f"聚合 {event.aggregate_id} 版本应为 {expected}，收到 {event.version}",
                )
            stream.append(event)
            self._event_ids.add(event.event_id)
            self._commit_order.append(event)
            committed.append(event)
        return tuple(committed)

    def load(self, aggregate_id: str) -> tuple[DomainEvent, ...]:
        return tuple(self._streams.get(aggregate_id, ()))

    def exists(self, event_id: str) -> bool:
        return event_id in self._event_ids

    def all_events(self) -> tuple[DomainEvent, ...]:
        """跨聚合事件，保持全局提交顺序（同一时刻下逻辑先后不乱）。"""
        return tuple(self._commit_order)

    # --- 幂等键 -------------------------------------------------------
    def remember(
        self, scope: str, idempotency_key: str, events: Iterable[DomainEvent]
    ) -> tuple[DomainEvent, ...]:
        produced = tuple(events)
        self._idempotency[(scope, idempotency_key)] = produced
        return produced

    def recall(self, scope: str, idempotency_key: str) -> tuple[DomainEvent, ...] | None:
        return self._idempotency.get((scope, idempotency_key))
