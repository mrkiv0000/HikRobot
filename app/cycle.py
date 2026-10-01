from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Protocol

from .models import AppSettings
from .protocol import CodeRecord, ProcessedMessage


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall_timestamp(self) -> str: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall_timestamp(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class CycleRecord:
    id: str
    x: float
    y: float
    a: float
    rounded_angle: int
    sn: int
    side: str
    camera: int

    @classmethod
    def from_code(cls, record: CodeRecord, camera: int) -> "CycleRecord":
        return cls(**asdict(record), camera=camera)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ActiveCycle:
    cycle_id: int
    started_monotonic: float
    deadline: float
    started_at: str
    duration: float
    expected: int
    selected_cameras: tuple[int, ...]
    records: OrderedDict[str, CycleRecord] = field(default_factory=OrderedDict)
    no_read_cameras: set[int] = field(default_factory=set)
    blockers: list[str] = field(default_factory=list)
    excess_reported: int = 0


@dataclass(frozen=True)
class CompletedCycle:
    cycle_id: int
    result: str
    started_at: str | None
    completed_at: str
    duration: float
    expected: int
    records: tuple[CycleRecord, ...]
    no_read_cameras: tuple[int, ...]
    reasons: tuple[str, ...]
    is_current: bool

    def to_dict(self) -> dict:
        return {
            "cycle_id": self.cycle_id,
            "result": self.result,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration": self.duration,
            "expected": self.expected,
            "received": len(self.records),
            "records": [record.to_dict() for record in self.records],
            "no_read_cameras": list(self.no_read_cameras),
            "reasons": list(self.reasons),
            "is_current": self.is_current,
        }


class CycleEngine:
    """Synchronous cycle state machine; networking and timer scheduling stay outside."""

    def __init__(self, settings: AppSettings, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()
        self._camera_count = settings.camera_count
        self._expected = settings.expected_codes
        self._duration = settings.collection_window
        self._next_cycle_id = 1
        self.active: ActiveCycle | None = None
        self.last_completed: CompletedCycle | None = None
        self.waiting_no_read: set[int] = set()

    @property
    def selected_cameras(self) -> tuple[int, ...]:
        return tuple(range(1, self._camera_count + 1))

    def apply_settings(self, settings: AppSettings, *, include_topology: bool) -> None:
        if include_topology:
            self._camera_count = settings.camera_count
        self._expected = settings.expected_codes
        self._duration = settings.collection_window

    def handle_codes(
        self,
        message: ProcessedMessage,
        connected: Callable[[int], bool],
    ) -> list[str]:
        events = self.advance()
        if message.camera not in self.selected_cameras or not message.records:
            return events

        if self.active is None:
            self._mark_previous_stale()
            now = self.clock.monotonic()
            selected = self.selected_cameras
            blockers: list[str] = []
            if not all(connected(camera) for camera in selected):
                blockers.append("Не все выбранные камеры подключены")
            self.active = ActiveCycle(
                cycle_id=self._next_cycle_id,
                started_monotonic=now,
                deadline=now + self._duration,
                started_at=message.timestamp,
                duration=self._duration,
                expected=self._expected,
                selected_cameras=selected,
                no_read_cameras=self.waiting_no_read.intersection(selected),
                blockers=blockers,
            )
            self._next_cycle_id += 1
            self.waiting_no_read.clear()
            events.append(
                f"Начат цикл {self.active.cycle_id}: ожидается {self.active.expected}, окно {self.active.duration:g} с"
            )
            if blockers:
                events.append(blockers[0])

        cycle = self.active
        assert cycle is not None
        for record in message.records:
            if record.id in cycle.records:
                first = cycle.records[record.id]
                events.append(
                    f"Цикл {cycle.cycle_id}: повтор ID {record.id}; сохранён первый результат камеры {first.camera}"
                )
                continue
            cycle.records[record.id] = CycleRecord.from_code(record, message.camera)
            events.append(
                f"Цикл {cycle.cycle_id}: добавлен ID {record.id} от камеры {message.camera}; "
                f"получено {len(cycle.records)} из {cycle.expected}"
            )
            if len(cycle.records) > cycle.expected and len(cycle.records) > cycle.excess_reported:
                cycle.excess_reported = len(cycle.records)
                events.append(
                    f"Ошибка: {self._quantity_error(cycle.expected, len(cycle.records), 'лишние коды')}; сбор продолжается"
                )
        return events

    def handle_no_read(self, camera: int) -> list[str]:
        events = self.advance()
        if camera not in self.selected_cameras:
            return events
        if self.active is not None:
            before = len(self.active.no_read_cameras)
            self.active.no_read_cameras.add(camera)
            if len(self.active.no_read_cameras) != before:
                events.append(f"Цикл {self.active.cycle_id}: NoRead от камеры {camera}")
            return events

        self._mark_previous_stale()
        before = len(self.waiting_no_read)
        self.waiting_no_read.add(camera)
        if len(self.waiting_no_read) == len(self.selected_cameras):
            cycle_id = self._next_cycle_id
            self._next_cycle_id += 1
            self.last_completed = CompletedCycle(
                cycle_id=cycle_id,
                result="error",
                started_at=None,
                completed_at=self.clock.wall_timestamp(),
                duration=0.0,
                expected=self._expected,
                records=(),
                no_read_cameras=tuple(sorted(self.waiting_no_read)),
                reasons=("Код не прочитан",),
                is_current=False,
            )
            self.waiting_no_read.clear()
            events.append(f"Цикл {cycle_id} завершён: Ошибка: Код не прочитан")
        elif len(self.waiting_no_read) != before:
            events.append(
                f"NoRead от {len(self.waiting_no_read)} из {len(self.selected_cameras)} камер; ожидание остальных данных"
            )
        return events

    def handle_diagnostic(self, camera: int, reason: str) -> list[str]:
        events = self.advance()
        if camera not in self.selected_cameras:
            return events
        if self.active is not None:
            blocker = f"Камера {camera}: {reason}"
            self._add_blocker(self.active, blocker)
            events.append(f"Цикл {self.active.cycle_id}: блокирующая ошибка — {blocker}")
        else:
            self._mark_previous_stale()
        return events

    def handle_connection_loss(self, camera: int) -> list[str]:
        events = self.advance()
        self.waiting_no_read.clear()
        if camera in self.selected_cameras and self.active is not None:
            blocker = f"Потеря связи с камерой {camera}"
            self._add_blocker(self.active, blocker)
            events.append(f"Цикл {self.active.cycle_id}: {blocker}; сбор продолжается")
        else:
            self._mark_previous_stale()
        return events

    def advance(self, now: float | None = None, *, expected_cycle_id: int | None = None) -> list[str]:
        current = self.clock.monotonic() if now is None else now
        if self.active is None:
            return []
        if expected_cycle_id is not None and self.active.cycle_id != expected_cycle_id:
            return []
        if current < self.active.deadline:
            return []
        return [self._complete_active()]

    def cancel(self, reason: str = "Сбор отменён пользователем") -> list[str]:
        self.waiting_no_read.clear()
        if self.active is None:
            self._mark_previous_stale()
            return []
        cycle = self.active
        self.active = None
        self.last_completed = CompletedCycle(
            cycle_id=cycle.cycle_id,
            result="cancelled",
            started_at=cycle.started_at,
            completed_at=self.clock.wall_timestamp(),
            duration=cycle.duration,
            expected=cycle.expected,
            records=(),
            no_read_cameras=(),
            reasons=(reason,),
            is_current=False,
        )
        return [f"Цикл {cycle.cycle_id}: {reason}"]

    def clear_waiting_after_connection_change(self) -> None:
        self.waiting_no_read.clear()
        if self.active is None:
            self._mark_previous_stale()

    def snapshot(self) -> dict:
        if self.active is not None:
            cycle = self.active
            received = len(cycle.records)
            if received > cycle.expected:
                phase = "excess"
                summary = f"Ошибка: {self._quantity_error(cycle.expected, received, 'лишние коды')}"
            elif received >= cycle.expected:
                phase = "collecting"
                summary = f"Получено {received} из {cycle.expected} — проверка продолжается"
            else:
                phase = "collecting"
                summary = f"Получено {received} из {cycle.expected} — сбор продолжается"
            return {
                "state": phase,
                "cycle_id": cycle.cycle_id,
                "started_at": cycle.started_at,
                "duration": cycle.duration,
                "remaining": max(0.0, cycle.deadline - self.clock.monotonic()),
                "expected": cycle.expected,
                "received": received,
                "summary": summary,
                "records": [record.to_dict() for record in cycle.records.values()],
                "no_read_cameras": sorted(cycle.no_read_cameras),
                "blockers": list(cycle.blockers),
                "selected_cameras": list(cycle.selected_cameras),
                "completed": False,
            }

        if self.waiting_no_read:
            return {
                "state": "waiting_no_read",
                "summary": (
                    f"NoRead от {len(self.waiting_no_read)} из {len(self.selected_cameras)} камер; "
                    "ожидание остальных данных"
                ),
                "expected": self._expected,
                "received": 0,
                "records": [],
                "no_read_cameras": sorted(self.waiting_no_read),
                "blockers": [],
                "completed": False,
            }

        if self.last_completed is not None:
            result = self.last_completed.to_dict()
            result.update(
                {
                    "state": self.last_completed.result,
                    "summary": self._completed_summary(self.last_completed),
                    "blockers": list(self.last_completed.reasons),
                    "completed": True,
                }
            )
            return result

        return {
            "state": "waiting",
            "summary": "Ожидание первого кода",
            "expected": self._expected,
            "received": 0,
            "records": [],
            "no_read_cameras": [],
            "blockers": [],
            "completed": False,
        }

    def _complete_active(self) -> str:
        cycle = self.active
        assert cycle is not None
        self.active = None
        received = len(cycle.records)
        reasons = list(cycle.blockers)
        if received < cycle.expected:
            reasons.insert(0, self._quantity_error(cycle.expected, received, "недостаточно кодов"))
        elif received > cycle.expected:
            reasons.insert(0, self._quantity_error(cycle.expected, received, "лишние коды"))
        result = "success" if not reasons else "error"
        self.last_completed = CompletedCycle(
            cycle_id=cycle.cycle_id,
            result=result,
            started_at=cycle.started_at,
            completed_at=self.clock.wall_timestamp(),
            duration=cycle.duration,
            expected=cycle.expected,
            records=tuple(cycle.records.values()),
            no_read_cameras=tuple(sorted(cycle.no_read_cameras)),
            reasons=tuple(reasons),
            is_current=result == "success",
        )
        self.waiting_no_read.clear()
        return f"Цикл {cycle.cycle_id} завершён: {self._completed_summary(self.last_completed)}"

    @staticmethod
    def _completed_summary(cycle: CompletedCycle) -> str:
        if cycle.result == "success":
            return f"Получено {len(cycle.records)} из {cycle.expected}"
        if cycle.result == "cancelled":
            return cycle.reasons[0]
        return "Ошибка: " + "; ".join(cycle.reasons)

    @staticmethod
    def _add_blocker(cycle: ActiveCycle, blocker: str) -> None:
        if blocker not in cycle.blockers:
            cycle.blockers.append(blocker)

    @staticmethod
    def _quantity_error(expected: int, received: int, suffix: str) -> str:
        word = "код" if expected == 1 else "кода" if 2 <= expected <= 4 else "кодов"
        return f"Ожидаем {expected} {word}, а получили {received} — {suffix}"

    def _mark_previous_stale(self) -> None:
        if self.last_completed is not None and self.last_completed.is_current:
            previous = self.last_completed
            self.last_completed = CompletedCycle(
                cycle_id=previous.cycle_id,
                result=previous.result,
                started_at=previous.started_at,
                completed_at=previous.completed_at,
                duration=previous.duration,
                expected=previous.expected,
                records=previous.records,
                no_read_cameras=previous.no_read_cameras,
                reasons=previous.reasons,
                is_current=False,
            )
