import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.cameras import CameraManager, ConnectionAlreadyRunning
from app.models import AppSettings, CameraSettings
from app.protocol import AssemblyEvent


class FakeWriter:
    def __init__(self):
        self.closed = False

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


def settings(*cameras):
    configured = list(cameras)
    configured.extend(CameraSettings() for _ in range(4 - len(configured)))
    return AppSettings(camera_count=len(cameras), cameras=configured)


def test_incoming_bytes_and_eof_are_recorded():
    async def scenario():
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        reader.feed_data(b"{NoRead}\r\n")
        reader.feed_eof()
        manager = CameraManager(AppSettings())

        with patch("app.cameras.asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            await manager.connect_all(settings(CameraSettings(ip="192.168.1.10")))
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        state = manager.snapshot()
        events = state["journal"]
        assert any(event["kind"] == "data" and event["message"] == "b'{NoRead}\\r\\n'" and event["byte_count"] == 10 for event in events)
        assert any(event["kind"] == "closed" for event in events)
        assert state["cameras"][0]["status"] == "disconnected"
        await manager.close()

    asyncio.run(scenario())


def test_failure_of_one_camera_does_not_block_another():
    async def scenario():
        good_reader = asyncio.StreamReader()
        good_writer = FakeWriter()

        async def open_connection(ip, port):
            if ip.endswith("10"):
                return good_reader, good_writer
            raise ConnectionRefusedError("refused")

        manager = CameraManager(AppSettings())
        configured = settings(
            CameraSettings(ip="192.168.1.10"),
            CameraSettings(ip="192.168.1.11"),
        )
        with patch("app.cameras.asyncio.open_connection", side_effect=open_connection):
            await manager.connect_all(configured)

        state = manager.snapshot()
        assert state["cameras"][0]["status"] == "connected"
        assert state["cameras"][1]["status"] == "disconnected"
        assert "refused" in state["cameras"][1]["error"]
        await manager.close()

    asyncio.run(scenario())


def test_repeated_connect_keeps_one_healthy_reader():
    async def scenario():
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        open_connection = AsyncMock(return_value=(reader, writer))
        manager = CameraManager(AppSettings())
        configured = settings(CameraSettings(ip="192.168.1.10"))

        with patch("app.cameras.asyncio.open_connection", new=open_connection):
            await manager.connect_all(configured)
            first_task = manager._slots[0].read_task
            await manager.connect_all(configured)

        assert open_connection.await_count == 1
        assert manager._slots[0].read_task is first_task
        assert not first_task.done()
        await manager.close()

    asyncio.run(scenario())


def test_zero_address_does_not_open_socket():
    async def scenario():
        manager = CameraManager(AppSettings())
        open_connection = AsyncMock()
        with patch("app.cameras.asyncio.open_connection", new=open_connection):
            await manager.connect_all(AppSettings())
        assert open_connection.await_count == 0
        assert manager.snapshot()["cameras"][0]["error"] == "IP-адрес не задан"
        await manager.close()

    asyncio.run(scenario())


def test_history_fifo_repeats_atomic_errors_and_polling():
    manager = CameraManager(AppSettings())
    slot = manager._slots[0]
    for index in range(12):
        payload = f"{{id:{index % 3:015d},p:({index},{index + 1}),a:0}}".encode()
        manager._publish_assembly_events(slot, [AssemblyEvent("message", data=payload)])
    manager._publish_assembly_events(slot, [AssemblyEvent("message", data=b"{NoRead}")])
    manager._publish_assembly_events(
        slot,
        [AssemblyEvent("message", data=b"{id:good,p:(1,2),a:0;id:bad,p:(1,2),a:361}")],
    )

    first = manager.snapshot()
    second = manager.snapshot()
    assert len(first["history"]) == 10
    assert first["history"] == second["history"]
    assert first["history"][0]["x"] == 2
    assert first["last_message"]["kind"] == "error"
    assert any(first["history"][index]["id"] == first["history"][index + 3]["id"] for index in range(7))


def test_disconnect_cancels_connection_and_allows_reconnect():
    async def scenario():
        started = asyncio.Event()

        async def hanging_connection(ip, port):
            started.set()
            await asyncio.Future()

        manager = CameraManager(AppSettings())
        configured = settings(CameraSettings(ip="192.168.1.10"))
        with patch("app.cameras.asyncio.open_connection", side_effect=hanging_connection):
            connecting = asyncio.create_task(manager.connect_all(configured))
            await started.wait()
            await manager.disconnect_all()
            await connecting

        state = manager.snapshot()
        assert state["cameras"][0]["status"] == "disconnected"
        assert not state["has_active_connections"]
        assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("camera-") and not task.done()]
        await manager.disconnect_all()

        reader = asyncio.StreamReader()
        writer = FakeWriter()
        with patch("app.cameras.asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            await manager.connect_all(configured)
        assert manager.snapshot()["cameras"][0]["status"] == "connected"
        await manager.close()

    asyncio.run(scenario())


def test_server_timer_completes_cycle_without_api_polling_or_more_messages():
    async def scenario():
        configured = AppSettings(expected_codes=1, collection_window=0.02)
        manager = CameraManager(configured)
        slot = manager._slots[0]
        slot.status = "connected"
        manager._publish_assembly_events(
            slot,
            [AssemblyEvent("message", data=b"{id:A,p:(1,2),a:0}")],
        )
        assert manager.snapshot()["cycle"]["state"] == "collecting"
        await asyncio.sleep(0.05)
        result = manager.snapshot()["cycle"]
        assert result["state"] == "success"
        assert result["received"] == 1
        completion_events = [
            event for event in manager.snapshot()["journal"]
            if event["kind"] == "cycle" and "завершён" in event["message"]
        ]
        assert len(completion_events) == 1
        await manager.close()

    asyncio.run(scenario())


def test_camera_topology_cannot_change_while_connected():
    async def scenario():
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        manager = CameraManager(AppSettings())
        configured = settings(CameraSettings(ip="192.168.1.10"))
        with patch("app.cameras.asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            await manager.connect_all(configured)
        changed = settings(CameraSettings(ip="192.168.1.11"))
        with pytest.raises(ConnectionAlreadyRunning, match="сначала нажмите"):
            await manager.connect_all(changed)
        assert manager.snapshot()["cameras"][0]["endpoint"]["ip"] == "192.168.1.10"
        await manager.close()

    asyncio.run(scenario())
