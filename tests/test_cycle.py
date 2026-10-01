from dataclasses import dataclass

import pytest

from app.cycle import CycleEngine
from app.models import AppSettings
from app.protocol import parse_message


@dataclass
class FakeClock:
    value: float = 100.0
    wall_index: int = 0

    def monotonic(self) -> float:
        return self.value

    def wall_timestamp(self) -> str:
        self.wall_index += 1
        return f"2026-10-01T12:00:00.{self.wall_index:03d}+03:00"

    def move(self, seconds: float) -> None:
        self.value += seconds


def settings(*, cameras=1, expected=1, duration=3.0):
    return AppSettings(camera_count=cameras, expected_codes=expected, collection_window=duration)


def codes(camera, *rows, timestamp="2026-10-01T12:00:00.000+03:00"):
    payload = ";".join(
        f"id:{identifier},p:({x},{y}),a:{angle}" for identifier, x, y, angle in rows
    )
    return parse_message(f"{{{payload}}}".encode(), camera, timestamp)


def connected_set(*numbers):
    available = set(numbers)
    return lambda camera: camera in available


def finish(engine, clock):
    active = engine.active
    assert active is not None
    clock.value = active.deadline
    engine.advance()
    return engine.snapshot()


def test_first_multi_code_packet_opens_one_window_and_waits_for_deadline():
    clock = FakeClock()
    engine = CycleEngine(settings(expected=2), clock)
    engine.handle_codes(codes(1, ("A", 1, 2, 0), ("B", 3, 4, 90)), connected_set(1))
    snapshot = engine.snapshot()
    assert snapshot["state"] == "collecting"
    assert snapshot["received"] == 2
    assert snapshot["summary"] == "Получено 2 из 2 — проверка продолжается"
    assert engine.last_completed is None
    assert finish(engine, clock)["state"] == "success"


def test_shortage_is_only_reported_at_deadline():
    clock = FakeClock()
    engine = CycleEngine(settings(expected=2), clock)
    engine.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1))
    assert "недостаточно" not in engine.snapshot()["summary"]
    result = finish(engine, clock)
    assert result["state"] == "error"
    assert "Ожидаем 2 кода, а получили 1 — недостаточно кодов" in result["summary"]


def test_second_id_before_deadline_succeeds_only_at_end():
    clock = FakeClock()
    engine = CycleEngine(settings(expected=2), clock)
    engine.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1))
    deadline = engine.active.deadline
    clock.move(2)
    engine.handle_codes(codes(1, ("B", 3, 4, 90)), connected_set(1))
    assert engine.active.deadline == deadline
    assert engine.last_completed is None
    assert finish(engine, clock)["state"] == "success"


def test_excess_is_immediate_but_collection_continues():
    clock = FakeClock()
    engine = CycleEngine(settings(expected=1), clock)
    engine.handle_codes(codes(1, ("A", 1, 2, 0), ("B", 3, 4, 90)), connected_set(1))
    assert engine.snapshot()["state"] == "excess"
    assert engine.active is not None
    result = finish(engine, clock)
    assert result["state"] == "error"
    assert "лишние коды" in result["summary"]


def test_repeats_keep_first_observation_and_do_not_extend_deadline():
    clock = FakeClock()
    engine = CycleEngine(settings(cameras=2), clock)
    engine.handle_codes(codes(1, ("A", 1, 2, 0), ("A", 9, 9, 180)), connected_set(1, 2))
    deadline = engine.active.deadline
    clock.move(1)
    engine.handle_codes(codes(2, ("A", 8, 8, 270)), connected_set(1, 2))
    record = next(iter(engine.active.records.values()))
    assert (record.camera, record.x, record.a, record.sn) == (1, 1.0, 0.0, 3)
    assert len(engine.active.records) == 1
    assert engine.active.deadline == deadline


def test_five_unique_ids_are_not_truncated_when_four_expected():
    clock = FakeClock()
    engine = CycleEngine(settings(expected=4), clock)
    engine.handle_codes(
        codes(1, *((str(index), index, index, 0) for index in range(1, 5))), connected_set(1)
    )
    engine.handle_codes(codes(1, ("5", 5, 5, 0)), connected_set(1))
    assert len(engine.active.records) == 5
    assert engine.snapshot()["state"] == "excess"


@pytest.mark.parametrize("camera_count", [2, 3, 4])
def test_partial_no_read_waits_and_duplicate_does_not_replace_other_camera(camera_count):
    engine = CycleEngine(settings(cameras=camera_count), FakeClock())
    engine.handle_no_read(1)
    engine.handle_no_read(1)
    assert engine.snapshot()["state"] == "waiting_no_read"
    assert engine.snapshot()["no_read_cameras"] == [1]
    for camera in range(2, camera_count):
        engine.handle_no_read(camera)
    assert engine.last_completed is None
    engine.handle_no_read(camera_count)
    assert engine.snapshot()["state"] == "error"
    assert engine.snapshot()["reasons"] == ["Код не прочитан"]


def test_one_camera_no_read_completes_immediately_without_window():
    engine = CycleEngine(settings(), FakeClock())
    engine.handle_no_read(1)
    result = engine.snapshot()
    assert result["state"] == "error"
    assert result["duration"] == 0
    assert engine.active is None


def test_partial_no_read_moves_into_cycle_started_by_code():
    clock = FakeClock()
    engine = CycleEngine(settings(cameras=2, expected=1), clock)
    engine.handle_no_read(2)
    engine.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1, 2))
    assert engine.active.no_read_cameras == {2}
    assert engine.active.deadline == pytest.approx(clock.value + 3)
    assert finish(engine, clock)["state"] == "success"


def test_no_read_inside_window_does_not_finish_or_create_second_result():
    clock = FakeClock()
    engine = CycleEngine(settings(cameras=2, expected=1), clock)
    engine.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1, 2))
    cycle_id = engine.active.cycle_id
    engine.handle_no_read(2)
    assert engine.active.cycle_id == cycle_id
    assert engine.last_completed is None
    result = finish(engine, clock)
    assert result["state"] == "success"
    assert result["no_read_cameras"] == [2]
    assert not engine.waiting_no_read


def test_missing_camera_at_start_blocks_success_but_silence_when_connected_does_not():
    clock = FakeClock()
    blocked = CycleEngine(settings(cameras=2), clock)
    blocked.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1))
    assert finish(blocked, clock)["state"] == "error"
    assert "Не все выбранные камеры подключены" in blocked.snapshot()["reasons"]

    clock = FakeClock()
    ready = CycleEngine(settings(cameras=2), clock)
    ready.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1, 2))
    assert finish(ready, clock)["state"] == "success"


def test_connection_loss_blocks_cycle_and_recovery_does_not_remove_reason():
    clock = FakeClock()
    engine = CycleEngine(settings(cameras=2), clock)
    engine.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1, 2))
    engine.handle_connection_loss(2)
    engine.handle_codes(codes(1, ("A", 8, 8, 90)), connected_set(1, 2))
    result = finish(engine, clock)
    assert result["state"] == "error"
    assert "Потеря связи с камерой 2" in result["reasons"]


def test_diagnostic_inside_blocks_but_error_outside_does_not_poison_next_cycle():
    clock = FakeClock()
    engine = CycleEngine(settings(), clock)
    engine.handle_diagnostic(1, "неверный угол")
    engine.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1))
    assert finish(engine, clock)["state"] == "success"

    engine.handle_codes(codes(1, ("B", 1, 2, 0)), connected_set(1))
    engine.handle_diagnostic(1, "повреждённое сообщение")
    engine.handle_codes(codes(1, ("C", 1, 2, 90)), connected_set(1))
    result = finish(engine, clock)
    assert result["state"] == "error"
    assert len(result["records"]) == 2
    assert "повреждённое сообщение" in result["summary"]


def test_event_at_deadline_finishes_old_cycle_then_opens_new():
    clock = FakeClock()
    engine = CycleEngine(settings(), clock)
    engine.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1))
    old_id = engine.active.cycle_id
    clock.value = engine.active.deadline
    events = engine.handle_codes(codes(1, ("B", 1, 2, 0)), connected_set(1))
    assert any(f"Цикл {old_id} завершён" in event for event in events)
    assert engine.active.cycle_id == old_id + 1
    assert list(engine.active.records) == ["B"]


def test_before_deadline_stays_old_and_after_deadline_starts_new():
    clock = FakeClock()
    engine = CycleEngine(settings(expected=2), clock)
    engine.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1))
    old_id = engine.active.cycle_id
    clock.value = engine.active.deadline - 0.0001
    engine.handle_codes(codes(1, ("B", 1, 2, 0)), connected_set(1))
    assert engine.active.cycle_id == old_id
    clock.value = engine.active.deadline + 0.0001
    engine.handle_codes(codes(1, ("C", 1, 2, 0)), connected_set(1))
    assert engine.active.cycle_id == old_id + 1
    assert list(engine.active.records) == ["C"]


def test_stale_timer_id_cannot_finish_new_cycle_twice():
    clock = FakeClock()
    engine = CycleEngine(settings(), clock)
    engine.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1))
    old_id = engine.active.cycle_id
    finish(engine, clock)
    engine.handle_codes(codes(1, ("B", 1, 2, 0)), connected_set(1))
    new_id = engine.active.cycle_id
    assert engine.advance(now=clock.value + 100, expected_cycle_id=old_id) == []
    assert engine.active.cycle_id == new_id


def test_cancel_clears_active_records_and_waiting_no_read():
    engine = CycleEngine(settings(cameras=2), FakeClock())
    engine.handle_no_read(2)
    engine.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1, 2))
    engine.cancel()
    result = engine.snapshot()
    assert result["state"] == "cancelled"
    assert result["records"] == []
    assert result["reasons"] == ["Сбор отменён пользователем"]
    assert not engine.waiting_no_read


def test_same_id_can_open_next_cycle_and_old_success_becomes_non_current():
    clock = FakeClock()
    engine = CycleEngine(settings(), clock)
    engine.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1))
    assert finish(engine, clock)["is_current"]
    engine.handle_codes(codes(1, ("A", 9, 9, 180)), connected_set(1))
    assert engine.active is not None
    assert next(iter(engine.active.records.values())).a == 180


def test_settings_snapshot_changes_only_next_cycle():
    clock = FakeClock()
    engine = CycleEngine(settings(expected=1, duration=3), clock)
    engine.handle_codes(codes(1, ("A", 1, 2, 0)), connected_set(1))
    old_deadline = engine.active.deadline
    engine.apply_settings(settings(expected=2, duration=8), include_topology=False)
    assert engine.active.expected == 1
    assert engine.active.duration == 3
    assert engine.active.deadline == old_deadline
    finish(engine, clock)
    engine.handle_codes(codes(1, ("B", 1, 2, 0)), connected_set(1))
    assert engine.active.expected == 2
    assert engine.active.duration == 8

