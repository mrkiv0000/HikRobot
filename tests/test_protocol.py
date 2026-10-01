import math

import pytest

from app.protocol import MessageAssembler, MessageFormatError, calculate_side, parse_message


REAL_MESSAGE = b"{id:405947004000041,p:(2163,1603),a:2;id:531952002000010,p:(2771,1586),a:1}"


def messages(events):
    return [event.data for event in events if event.kind == "message"]


def errors(events):
    return [event.error for event in events if event.kind == "error"]


@pytest.mark.parametrize("split", range(1, len(REAL_MESSAGE)))
def test_message_split_at_every_byte_position(split):
    assembler = MessageAssembler()
    events = assembler.feed(REAL_MESSAGE[:split]) + assembler.feed(REAL_MESSAGE[split:])
    assert messages(events) == [REAL_MESSAGE]
    assert not errors(events)


def test_one_two_and_four_codes_are_all_parsed():
    records = [f"id:{index:015d},p:({index + .25},{-index}),a:{index * 90}" for index in range(1, 5)]
    for count in (1, 2, 4):
        result = parse_message(("{" + ";".join(records[:count]) + "}").encode(), camera=2)
        assert result.kind == "codes"
        assert len(result.records) == count
        assert [record.id for record in result.records] == [f"{index:015d}" for index in range(1, count + 1)]


def test_real_camera_message_keeps_xy_order_and_parses_both_codes():
    result = parse_message(REAL_MESSAGE, camera=1)
    assert result.kind == "codes"
    assert [(record.id, record.x, record.y, record.a, record.sn) for record in result.records] == [
        ("405947004000041", 2163.0, 1603.0, 2.0, 3),
        ("531952002000010", 2771.0, 1586.0, 1.0, 3),
    ]


def test_multiple_messages_no_read_and_incomplete_tail():
    assembler = MessageAssembler()
    first = b"{id:001,p:(1,2),a:0}"
    second = b"{id:002,p:(3,4),a:90}"
    events = assembler.feed(first + b"\r\n{NoRead}" + second + b"{id:tail")
    assert messages(events) == [first, b"{NoRead}", second]
    assert assembler.has_incomplete_message
    assert errors(assembler.finish())


def test_independent_camera_buffers_do_not_mix():
    camera_1 = MessageAssembler()
    camera_2 = MessageAssembler()
    assert messages(camera_1.feed(b"{id:one,")) == []
    assert messages(camera_2.feed(b"{No")) == []
    assert messages(camera_1.feed(b"p:(1,2),a:0}")) == [b"{id:one,p:(1,2),a:0}"]
    assert messages(camera_2.feed(b"Read}")) == [b"{NoRead}"]


def test_reset_drops_incomplete_tail():
    assembler = MessageAssembler()
    assembler.feed(b"{id:old")
    assembler.reset()
    assert messages(assembler.feed(b"{NoRead}")) == [b"{NoRead}"]


def test_assembly_diagnostics_and_recovery():
    assembler = MessageAssembler(max_message_size=12)
    events = assembler.feed(b"junk{broken{id:x,p:(1,2),a:0}{012345678901234}{NoRead}")
    assert any("Посторонние" in error for error in errors(events))
    assert any("Новая '{'" in error for error in errors(events))
    assert any("превышает лимит" in error for error in errors(events))
    assert messages(events)[-1] == b"{NoRead}"


def test_ids_trailing_separator_and_empty_record_rules():
    leading = parse_message(b"{id:000000000000001,p:(1,2),a:0;}", 1)
    assert leading.kind == "codes"
    assert leading.records[0].id == "000000000000001"
    assert parse_message(b"{id:1234567890123456,p:(1,2),a:0}", 1).kind == "error"
    assert "длиннее" in parse_message(b"{id:1234567890123456,p:(1,2),a:0}", 1).error
    assert "ID" in parse_message(b"{id:,p:(1,2),a:0}", 1).error
    assert parse_message(b"{id:a,p:(1,2),a:0;;id:b,p:(1,2),a:0}", 1).kind == "error"
    assert parse_message(b"{}", 1).kind == "error"


def test_no_read_is_exact_and_separate():
    assert parse_message(b"{  NoRead  }", 1).kind == "no_read"
    assert parse_message(b"{noread}", 1).kind == "error"


def test_numbers_full_record_validation_and_atomic_failure():
    good = b"{id:A-1,p:(-1.25,+2e2),a:45.5}"
    record = parse_message(good, 1).records[0]
    assert (record.x, record.y, record.a, record.sn) == (-1.25, 200.0, 45.5, 1)
    assert parse_message(b"{id:x,p:(NaN,2),a:0}", 1).kind == "error"
    assert parse_message(b"{id:x,p:(1,Infinity),a:0}", 1).kind == "error"
    assert parse_message(b"{id:x,p:(1,2),a:0,sn:3}", 1).kind == "error"
    mixed = parse_message(b"{id:good,p:(1,2),a:0;id:bad,p:(1,2),a:361}", 1)
    assert mixed.kind == "error"
    assert mixed.records == ()


@pytest.mark.parametrize(
    ("angle", "rounded", "sn"),
    [
        (0, 0, 3), (45, 45, 3), (45.4, 45, 3), (45.5, 46, 1), (46, 46, 1),
        (90, 90, 1), (135, 135, 1), (135.5, 136, 4), (136, 136, 4), (180, 180, 4),
        (225, 225, 4), (225.5, 226, 2), (226, 226, 2), (270, 270, 2), (315, 315, 2),
        (315.5, 316, 3), (316, 316, 3), (360, 360, 3),
        (44.5, 44, 3), (134.5, 134, 1), (224.5, 224, 4), (314.5, 314, 2),
        (45.4999999, 46, 1),
    ],
)
def test_siemens_binary32_rounding(angle, rounded, sn):
    assert calculate_side(angle)[:2] == (rounded, sn)


@pytest.mark.parametrize("angle", [-1, -0.000001, 360.000001, 361, math.nan, math.inf, -math.inf])
def test_invalid_angles(angle):
    with pytest.raises(MessageFormatError):
        calculate_side(angle)
