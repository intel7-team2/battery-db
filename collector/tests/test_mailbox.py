import uuid

import pytest

from battery_collector import mailbox as m
from battery_collector.config import ConfigError, parse_mailbox
from battery_collector.events import EVENT_NAMESPACE, entity_id, event_uuid
from battery_collector.plc.fake import FakePlc


def words_after(mb, plc):
    return plc.read_words(mb.base, mb.size)


def test_size_matches_draft(mb):
    # 2a-data-design.md: 헤더 4 + 8 x 12 = D2700~D2799
    assert mb.size == 100
    assert mb.base + mb.size - 1 == 2799


def test_decode_32bit_low_word_first(mb, clock):
    plc = FakePlc(mb, clock=clock)
    plc.push({"event": 2, "station": 2, "cell_no": 70000, "lot": 4})
    r = m.read_new(mb, words_after(mb, plc), 0)
    assert [x.fields["cell_no"] for x in r.records] == [70000]
    assert r.records[0].fields["lot"] == 4 and r.lost == 0 and r.torn == []


def test_pending_wraps():
    assert m.pending(65534, 1) == 3
    assert m.pending(5, 5) == 0


def test_lost_when_more_than_ring(mb, clock):
    plc = FakePlc(mb, clock=clock)
    # ACK를 흉내 내서 PLC가 계속 쓰게 하고, 수집기는 0에 머문 상황
    for i in range(1, 12):
        plc.push({"event": 1, "station": 1, "cell_no": i})
        plc.write_word(mb.base + mb.read_seq, i)
    r = m.read_new(mb, words_after(mb, plc), 0)
    assert r.lost == 3
    assert [x.seq for x in r.records] == list(range(4, 12))


def test_torn_record_stops_reading(mb, clock):
    plc = FakePlc(mb, clock=clock)
    plc.push({"event": 1, "station": 1, "cell_no": 1})
    plc.push({"event": 1, "station": 1, "cell_no": 2})
    w = words_after(mb, plc)
    w[mb.record_base + 2 * mb.record_words] = 999  # 순번 2 레코드가 아직 덜 써진 것처럼
    r = m.read_new(mb, w, 0)
    assert [x.seq for x in r.records] == [1] and r.torn == [2]


def test_ring_wraps_at_16bit(mb, clock):
    plc = FakePlc(mb, clock=clock)
    plc.write_word(mb.base + mb.write_seq, 65534)
    plc.write_word(mb.base + mb.read_seq, 65534)
    for i in range(3):
        plc.push({"event": 1, "station": 1, "cell_no": i + 1})
    r = m.read_new(mb, words_after(mb, plc), 65534)
    assert [x.seq for x in r.records] == [65535, 0, 1]
    total = 65534
    totals = [total := m.unwrap(total, x.seq) for x in r.records]
    assert totals == [65535, 65536, 65537]


def test_event_uuid_matches_api_rule():
    ns = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/intel7-team2/battery-db/event")
    assert EVENT_NAMESPACE == ns
    assert event_uuid(12, 37) == uuid.uuid5(ns, "run:12/seq:37")


def test_entity_ids(mb):
    assert entity_id("cell", 12, {"cell_no": 37}, mb.grades) == "R0012-000037"
    assert entity_id("box", 12, {"cell_no": 3, "result": 1}, mb.grades) == "B0012-A003"
    assert entity_id("cell", 12, {"cell_no": 0}, mb.grades) is None


def test_config_rejects_field_outside_record(mb):
    raw = {"base": 2700, "write_seq": 0, "read_seq": 1, "overflow": 2, "record_base": 4,
           "record_words": 12, "record_count": 8,
           "fields": {"seq": [0, 1], "event": [1, 1], "station": [2, 1], "cell_no": [11, 2]},
           "stations": {"1": "2A-IN"}, "events": {"1": {"type": "cell.created"}}}
    with pytest.raises(ConfigError):
        parse_mailbox(raw)
