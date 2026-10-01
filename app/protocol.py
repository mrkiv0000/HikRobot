from __future__ import annotations

import math
import re
import struct
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal


MAX_MESSAGE_SIZE = 64 * 1024
_WHITESPACE = frozenset(b" \t\r\n")
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_RECORD = re.compile(
    rf"\s*id\s*:\s*(?P<id>[^\s,;:{{}}()]*)\s*,\s*"
    rf"p\s*:\s*\(\s*(?P<x>{_NUMBER})\s*,\s*(?P<y>{_NUMBER})\s*\)\s*,\s*"
    rf"a\s*:\s*(?P<a>{_NUMBER})\s*",
    re.ASCII,
)


@dataclass(frozen=True)
class AssemblyEvent:
    kind: Literal["message", "error"]
    data: bytes | None = None
    error: str | None = None


class MessageAssembler:
    """Extract brace-delimited messages from one camera's byte stream."""

    def __init__(self, max_message_size: int = MAX_MESSAGE_SIZE) -> None:
        self.max_message_size = max_message_size
        self._current: bytearray | None = None
        self._outside = bytearray()
        self._discard_until_start = False

    @property
    def has_incomplete_message(self) -> bool:
        return self._current is not None

    def feed(self, data: bytes) -> list[AssemblyEvent]:
        events: list[AssemblyEvent] = []
        for byte in data:
            if self._discard_until_start:
                if byte == ord("{"):
                    self._discard_until_start = False
                    self._current = bytearray(b"{")
                continue

            if self._current is None:
                if byte == ord("{"):
                    self._flush_outside(events)
                    self._current = bytearray(b"{")
                elif byte not in _WHITESPACE:
                    self._outside.append(byte)
                elif self._outside:
                    self._outside.append(byte)
                continue

            if byte == ord("{"):
                events.append(
                    AssemblyEvent(
                        "error",
                        error=f"Новая '{{' до завершения сообщения; отброшен фрагмент {bytes(self._current)!r}",
                    )
                )
                self._current = bytearray(b"{")
                continue

            self._current.append(byte)
            if len(self._current) > self.max_message_size:
                events.append(
                    AssemblyEvent(
                        "error",
                        error=f"Сообщение превышает лимит {self.max_message_size} байт; фрагмент отброшен",
                    )
                )
                self._current = None
                self._discard_until_start = True
            elif byte == ord("}"):
                events.append(AssemblyEvent("message", data=bytes(self._current)))
                self._current = None

        self._flush_outside(events)
        return events

    def finish(self) -> list[AssemblyEvent]:
        events: list[AssemblyEvent] = []
        self._flush_outside(events)
        if self._current is not None:
            events.append(
                AssemblyEvent(
                    "error",
                    error=f"Соединение закрыто с незавершённым сообщением {bytes(self._current)!r}",
                )
            )
        self.reset()
        return events

    def reset(self) -> None:
        self._current = None
        self._outside.clear()
        self._discard_until_start = False

    def _flush_outside(self, events: list[AssemblyEvent]) -> None:
        if self._outside:
            fragment = bytes(self._outside).rstrip(b" \t\r\n")
            if fragment:
                events.append(
                    AssemblyEvent(
                        "error",
                        error=f"Посторонние данные вне фигурных скобок: {fragment!r}",
                    )
                )
            self._outside.clear()


@dataclass(frozen=True)
class CodeRecord:
    id: str
    x: float
    y: float
    a: float
    rounded_angle: int
    sn: int
    side: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProcessedMessage:
    camera: int
    timestamp: str
    raw: str
    kind: Literal["codes", "no_read", "error"]
    records: tuple[CodeRecord, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "camera": self.camera,
            "timestamp": self.timestamp,
            "raw": self.raw,
            "kind": self.kind,
            "records": [record.to_dict() for record in self.records],
            "error": self.error,
        }


class MessageFormatError(ValueError):
    pass


def now_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def parse_message(message: bytes, camera: int, timestamp: str | None = None) -> ProcessedMessage:
    processed_at = timestamp or now_timestamp()
    try:
        text = message.decode("ascii")
    except UnicodeDecodeError as exc:
        return ProcessedMessage(camera, processed_at, repr(message), "error", error=f"Сообщение не является ASCII: {exc}")

    try:
        if len(text) < 2 or not text.startswith("{") or not text.endswith("}"):
            raise MessageFormatError("Сообщение должно начинаться с '{' и заканчиваться '}'")
        payload = text[1:-1].strip()
        if payload == "NoRead":
            return ProcessedMessage(camera, processed_at, text, "no_read")
        if not payload:
            raise MessageFormatError("Пустое сообщение не является NoRead")

        parts = payload.split(";")
        if parts[-1].strip() == "":
            parts.pop()
        if not parts or any(not part.strip() for part in parts):
            raise MessageFormatError("Пустая QR-запись между разделителями ';'")

        records = tuple(_parse_record(part, index + 1) for index, part in enumerate(parts))
        return ProcessedMessage(camera, processed_at, text, "codes", records=records)
    except MessageFormatError as exc:
        return ProcessedMessage(camera, processed_at, text, "error", error=str(exc))


def _parse_record(text: str, position: int) -> CodeRecord:
    match = _RECORD.fullmatch(text)
    if match is None:
        raise MessageFormatError(f"Запись {position}: неверный формат или неизвестные/повторные поля")

    identifier = match.group("id")
    if not identifier:
        raise MessageFormatError(f"Запись {position}: ID не должен быть пустым")
    if len(identifier) > 15:
        raise MessageFormatError(f"Запись {position}: ID длиннее 15 символов")

    x = _finite_number(match.group("x"), position, "X")
    y = _finite_number(match.group("y"), position, "Y")
    a = _finite_number(match.group("a"), position, "A")
    try:
        rounded_angle, sn, side = calculate_side(a)
    except MessageFormatError as exc:
        raise MessageFormatError(f"Запись {position}: {exc}") from exc
    return CodeRecord(identifier, x, y, a, rounded_angle, sn, side)


def _finite_number(text: str, position: int, field: str) -> float:
    try:
        value = float(text)
    except (ValueError, OverflowError) as exc:
        raise MessageFormatError(f"Запись {position}: поле {field} не является числом") from exc
    if not math.isfinite(value):
        raise MessageFormatError(f"Запись {position}: поле {field} должно быть конечным числом")
    return value


def calculate_side(angle: float) -> tuple[int, int, str]:
    if not math.isfinite(angle):
        raise MessageFormatError("Угол A должен быть конечным числом")
    if angle < 0 or angle > 360:
        raise MessageFormatError("Угол вне диапазона 0–360")

    binary32 = struct.unpack("!f", struct.pack("!f", angle))[0]
    rounded = round(binary32)
    if 0 <= rounded <= 45 or 316 <= rounded <= 360:
        return rounded, 3, "Лево"
    if 46 <= rounded <= 135:
        return rounded, 1, "Верх"
    if 136 <= rounded <= 225:
        return rounded, 4, "Право"
    if 226 <= rounded <= 315:
        return rounded, 2, "Низ"
    raise MessageFormatError("Угол вне диапазона 0–360")
