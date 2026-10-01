from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .cycle import CycleEngine
from .models import AppSettings, CameraSettings
from .protocol import MessageAssembler, ProcessedMessage, now_timestamp, parse_message


LOGGER = logging.getLogger("uvicorn.error.hikrobot")
READ_SIZE = 8192
CONNECT_TIMEOUT = 5.0
JOURNAL_LIMIT = 200
HISTORY_LIMIT = 10


class ConnectionAlreadyRunning(RuntimeError):
    pass


class DisconnectionAlreadyRunning(RuntimeError):
    pass


@dataclass
class CameraSlot:
    number: int
    selected: bool = False
    status: str = "unused"
    error: str | None = None
    endpoint: tuple[str, int] | None = None
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    connect_task: asyncio.Task[None] | None = None
    read_task: asyncio.Task[None] | None = None
    generation: int = 0
    assembler: MessageAssembler = field(default_factory=MessageAssembler)


class CameraManager:
    def __init__(self, initial_settings: AppSettings) -> None:
        self._slots = [CameraSlot(number=index + 1) for index in range(4)]
        self._journal: deque[dict[str, Any]] = deque(maxlen=JOURNAL_LIMIT)
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LIMIT)
        self._last_message: ProcessedMessage | None = None
        self._event_id = 0
        self._message_id = 0
        self._applied_settings = initial_settings.model_copy(deep=True)
        self._cycle = CycleEngine(initial_settings)
        self._cycle_timer: asyncio.Task[None] | None = None
        self._cycle_timer_id: int | None = None
        self._command_lock = asyncio.Lock()
        self._connection_in_progress = False
        self._disconnection_in_progress = False
        self._connect_operation = 0
        self._reception_stopped = False
        for index, slot in enumerate(self._slots):
            slot.selected = index < initial_settings.camera_count
            slot.status = "disconnected" if slot.selected else "unused"

    @property
    def connection_in_progress(self) -> bool:
        return self._connection_in_progress

    @property
    def disconnection_in_progress(self) -> bool:
        return self._disconnection_in_progress

    def add_program_event(self, kind: str, message: str) -> None:
        self._add_event("Программа", kind, message)

    def _add_event(
        self,
        source: str,
        kind: str,
        message: str,
        *,
        byte_count: int | None = None,
    ) -> None:
        self._event_id += 1
        event: dict[str, Any] = {
            "id": self._event_id,
            "timestamp": now_timestamp(),
            "source": source,
            "kind": kind,
            "message": message,
        }
        if byte_count is not None:
            event["byte_count"] = byte_count
        self._journal.append(event)

        if kind in {"connection", "closed", "parsed", "no_read"}:
            LOGGER.info("%s: %s", source, message)
        elif kind in {"error", "assembly_error", "parse_error"}:
            LOGGER.error("%s: %s", source, message)

    def snapshot(self) -> dict[str, Any]:
        cameras = []
        for slot in self._slots:
            cameras.append(
                {
                    "number": slot.number,
                    "selected": slot.selected,
                    "status": slot.status,
                    "error": slot.error,
                    "endpoint": (
                        {"ip": slot.endpoint[0], "port": slot.endpoint[1]}
                        if slot.endpoint is not None
                        else None
                    ),
                }
            )
        last = self._last_message.to_dict() if self._last_message is not None else None
        if last is not None:
            source = self._slots[last["camera"] - 1]
            last["source_connected"] = source.status == "connected"
            last["reception_stopped"] = self._reception_stopped
        return {
            "cameras": cameras,
            "journal": list(self._journal),
            "history": list(self._history),
            "last_message": last,
            "cycle": self._cycle.snapshot(),
            "connection_in_progress": self.connection_in_progress,
            "disconnection_in_progress": self.disconnection_in_progress,
            "has_active_connections": self._has_active_connections(),
        }

    def update_process_settings(self, settings: AppSettings) -> None:
        self._applied_settings.expected_codes = settings.expected_codes
        self._applied_settings.collection_window = settings.collection_window
        self._cycle.apply_settings(settings, include_topology=False)

    def _has_active_connections(self) -> bool:
        return any(
            slot.status in {"connecting", "connected"}
            or (slot.connect_task is not None and not slot.connect_task.done())
            or (slot.read_task is not None and not slot.read_task.done())
            for slot in self._slots
        )

    def _same_topology(self, settings: AppSettings) -> bool:
        return (
            settings.camera_count == self._applied_settings.camera_count
            and settings.cameras == self._applied_settings.cameras
        )

    async def connect_all(self, settings: AppSettings) -> None:
        if self._connection_in_progress:
            raise ConnectionAlreadyRunning("Подключение уже выполняется")
        if self._disconnection_in_progress:
            raise ConnectionAlreadyRunning("Сначала дождитесь завершения отключения")
        if (self._has_active_connections() or self._cycle.active is not None) and not self._same_topology(settings):
            raise ConnectionAlreadyRunning(
                "Чтобы изменить количество или адреса камер, сначала нажмите «Отключить все камеры»"
            )

        async with self._command_lock:
            if self._connection_in_progress:
                raise ConnectionAlreadyRunning("Подключение уже выполняется")
            self._connection_in_progress = True
            self._connect_operation += 1
            operation = self._connect_operation
            self._reception_stopped = False
            connection_change = (
                not self._same_topology(settings)
                or any(
                    index < settings.camera_count and slot.status != "connected"
                    for index, slot in enumerate(self._slots)
                )
            )
            self._applied_settings = settings.model_copy(deep=True)
            self._cycle.apply_settings(settings, include_topology=True)
            if connection_change:
                self._cycle.clear_waiting_after_connection_change()
            attempts: list[asyncio.Task[None]] = []
            for index, slot in enumerate(self._slots):
                selected = index < settings.camera_count
                camera = settings.cameras[index]
                if not selected:
                    await self._disconnect_slot(slot)
                    slot.selected = False
                    slot.status = "unused"
                    slot.error = None
                    slot.endpoint = None
                    continue

                slot.selected = True
                endpoint = (camera.ip, camera.port)
                if camera.ip == "0.0.0.0":
                    await self._disconnect_slot(slot)
                    slot.status = "disconnected"
                    slot.error = "IP-адрес не задан"
                    slot.endpoint = None
                    self._add_event(f"Камера {slot.number}", "error", slot.error)
                    continue

                if self._is_healthy_at(slot, endpoint):
                    continue

                await self._disconnect_slot(slot)
                slot.endpoint = endpoint
                slot.status = "connecting"
                slot.error = None
                slot.generation += 1
                generation = slot.generation
                task = asyncio.create_task(
                    self._open_slot(slot, camera, generation),
                    name=f"camera-{slot.number}-connect",
                )
                slot.connect_task = task
                attempts.append(task)

        try:
            if attempts:
                await asyncio.gather(*attempts, return_exceptions=True)
        finally:
            if self._connect_operation == operation:
                self._connection_in_progress = False

    @staticmethod
    def _is_healthy_at(slot: CameraSlot, endpoint: tuple[str, int]) -> bool:
        return (
            slot.status == "connected"
            and slot.endpoint == endpoint
            and slot.writer is not None
            and not slot.writer.is_closing()
            and slot.read_task is not None
            and not slot.read_task.done()
        )

    async def _open_slot(self, slot: CameraSlot, camera: CameraSettings, generation: int) -> None:
        source = f"Камера {slot.number}"
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(camera.ip, camera.port), timeout=CONNECT_TIMEOUT
            )
        except asyncio.CancelledError:
            return
        except (OSError, asyncio.TimeoutError) as exc:
            if slot.generation == generation:
                slot.status = "disconnected"
                slot.error = f"Не удалось подключиться: {self._describe_error(exc)}"
                self._add_event(source, "error", slot.error)
            return
        finally:
            if slot.connect_task is asyncio.current_task():
                slot.connect_task = None

        if slot.generation != generation:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return

        slot.assembler.reset()
        slot.reader = reader
        slot.writer = writer
        slot.status = "connected"
        slot.error = None
        self._add_event(source, "connection", f"TCP-соединение установлено с {camera.ip}:{camera.port}")
        slot.read_task = asyncio.create_task(
            self._read_loop(slot, generation), name=f"camera-{slot.number}-reader"
        )

    async def _read_loop(self, slot: CameraSlot, generation: int) -> None:
        source = f"Камера {slot.number}"
        try:
            while True:
                assert slot.reader is not None
                data = await slot.reader.read(READ_SIZE)
                if slot.generation != generation:
                    break
                if not data:
                    self._publish_assembly_events(slot, slot.assembler.finish())
                    slot.status = "disconnected"
                    slot.error = "Соединение закрыто удалённой стороной"
                    self._add_event(source, "closed", slot.error)
                    self._publish_cycle_events(self._cycle.handle_connection_loss(slot.number))
                    self._sync_cycle_timer()
                    break
                self._add_event(source, "data", repr(data), byte_count=len(data))
                self._publish_assembly_events(slot, slot.assembler.feed(data))
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.IncompleteReadError) as exc:
            if slot.generation == generation:
                self._publish_assembly_events(slot, slot.assembler.finish())
                slot.status = "disconnected"
                slot.error = f"Ошибка чтения: {self._describe_error(exc)}"
                self._add_event(source, "error", slot.error)
                self._publish_cycle_events(self._cycle.handle_connection_loss(slot.number))
                self._sync_cycle_timer()
        finally:
            if slot.generation == generation:
                writer = slot.writer
                slot.reader = None
                slot.writer = None
                slot.read_task = None
                slot.assembler.reset()
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass

    def _publish_assembly_events(self, slot: CameraSlot, events: list) -> None:
        source = f"Камера {slot.number}"
        for event in events:
            if event.kind == "error":
                self._add_event(source, "assembly_error", event.error or "Ошибка сборки сообщения")
                self._publish_cycle_events(
                    self._cycle.handle_diagnostic(
                        slot.number, event.error or "Ошибка сборки сообщения"
                    )
                )
                self._sync_cycle_timer()
                continue
            assert event.data is not None
            result = parse_message(event.data, slot.number)
            self._message_id += 1
            self._last_message = result
            if result.kind == "codes":
                for record_index, record in enumerate(result.records):
                    self._history.append(
                        {
                            "message_id": self._message_id,
                            "record_index": record_index,
                            "timestamp": result.timestamp,
                            "camera": slot.number,
                            **record.to_dict(),
                        }
                    )
                self._add_event(source, "parsed", f"Полное сообщение разобрано; кодов: {len(result.records)}")
                self._publish_cycle_events(
                    self._cycle.handle_codes(
                        result,
                        connected=lambda camera: self._slots[camera - 1].status == "connected",
                    )
                )
            elif result.kind == "no_read":
                self._add_event(source, "no_read", "Получено полное сообщение NoRead")
                self._publish_cycle_events(self._cycle.handle_no_read(slot.number))
            else:
                self._add_event(source, "parse_error", f"Ошибка разбора полного сообщения: {result.error}")
                self._publish_cycle_events(
                    self._cycle.handle_diagnostic(
                        slot.number, result.error or "Ошибка разбора полного сообщения"
                    )
                )
            self._sync_cycle_timer()

    def _publish_cycle_events(self, events: list[str]) -> None:
        for message in events:
            self._add_event("Программа", "cycle", message)

    def _sync_cycle_timer(self) -> None:
        active = self._cycle.active
        if active is None:
            self._cancel_cycle_timer()
            return
        if (
            self._cycle_timer is not None
            and not self._cycle_timer.done()
            and self._cycle_timer_id == active.cycle_id
        ):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._cancel_cycle_timer()
        self._cycle_timer_id = active.cycle_id
        self._cycle_timer = asyncio.create_task(
            self._run_cycle_timer(active.cycle_id, active.deadline),
            name=f"cycle-{active.cycle_id}-timer",
        )

    async def _run_cycle_timer(self, cycle_id: int, deadline: float) -> None:
        try:
            delay = max(0.0, deadline - self._cycle.clock.monotonic())
            await asyncio.sleep(delay)
            self._publish_cycle_events(
                self._cycle.advance(expected_cycle_id=cycle_id)
            )
        except asyncio.CancelledError:
            raise
        finally:
            if self._cycle_timer is asyncio.current_task():
                self._cycle_timer = None
                self._cycle_timer_id = None

    def _cancel_cycle_timer(self) -> None:
        timer = self._cycle_timer
        self._cycle_timer = None
        self._cycle_timer_id = None
        if timer is not None and timer is not asyncio.current_task() and not timer.done():
            timer.cancel()

    async def disconnect_all(self) -> None:
        if self._disconnection_in_progress:
            raise DisconnectionAlreadyRunning("Отключение уже выполняется")
        self._disconnection_in_progress = True
        try:
            async with self._command_lock:
                self._connect_operation += 1
                self._publish_cycle_events(self._cycle.cancel())
                cycle_timer = self._cycle_timer
                self._cancel_cycle_timer()
                if cycle_timer is not None:
                    await asyncio.gather(cycle_timer, return_exceptions=True)
                for slot in self._slots:
                    await self._disconnect_slot(slot, log_close=False)
                    slot.error = None
                    slot.endpoint = None
                    slot.status = "disconnected" if slot.selected else "unused"
                self._reception_stopped = True
                self._add_event("Программа", "closed", "Все камеры отключены пользователем")
        finally:
            self._connection_in_progress = False
            self._disconnection_in_progress = False

    async def _disconnect_slot(self, slot: CameraSlot, *, log_close: bool = True) -> None:
        was_active = slot.writer is not None or slot.connect_task is not None or slot.read_task is not None
        slot.generation += 1
        connect_task = slot.connect_task
        read_task = slot.read_task
        writer = slot.writer
        slot.connect_task = None
        slot.reader = None
        slot.writer = None
        slot.read_task = None
        slot.assembler.reset()

        tasks = []
        for task in (connect_task, read_task):
            if task is not None and task is not asyncio.current_task():
                if not task.done():
                    task.cancel()
                tasks.append(task)
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if was_active and log_close:
            self._add_event(f"Камера {slot.number}", "closed", "TCP-соединение закрыто программой")

    async def close(self) -> None:
        timer = self._cycle_timer
        self._cancel_cycle_timer()
        if timer is not None:
            await asyncio.gather(timer, return_exceptions=True)
        async with self._command_lock:
            for slot in self._slots:
                await self._disconnect_slot(slot)
                slot.status = "disconnected" if slot.selected else "unused"

    @staticmethod
    def _describe_error(exc: BaseException) -> str:
        if isinstance(exc, asyncio.TimeoutError):
            return "истекло время ожидания (5 с)"
        return str(exc) or exc.__class__.__name__
