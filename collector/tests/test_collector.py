"""가짜 PLC + 실제 outbox로 수집 루프 전체."""
from datetime import datetime, timezone

from battery_collector.collector import TORN_GIVE_UP, Collector
from battery_collector.outbox import Outbox
from battery_collector.plc.fake import FakePlc

T0 = datetime(2026, 11, 26, tzinfo=timezone.utc)


def make(mb, tmp_path, clock, run_id=12, **kw):
    kw.setdefault("cells_per_min", 60)
    plc = FakePlc(mb, clock=clock, **kw)
    ob = Outbox(tmp_path / "ob.sqlite3")
    return plc, ob, Collector(mb, "2A", plc, ob, run_id, clock=lambda: T0)


def run_for(col, clock, seconds, step=0.1):
    end = clock.t + seconds
    while clock.t < end:
        clock.t += step
        col.poll_once()


def all_events(ob):
    return ob.next_batch(100000)


def test_collects_full_lifecycle_in_order(mb, tmp_path, clock):
    plc, ob, col = make(mb, tmp_path, clock)
    run_for(col, clock, 120)
    ev = all_events(ob)
    seqs = [e["seq"] for e in ev]
    assert seqs == list(range(1, len(ev) + 1))  # 빠짐·중복 없음
    assert plc.dropped == 0 and col.c.lost == 0
    assert len(ev) == plc.written
    types = {e["event_type"] for e in ev}
    assert {"cell.created", "cell.id_read", "cell.test_end", "cell.sorted", "cell.packed", "box.closed"} <= types
    end = next(e for e in ev if e["event_type"] == "cell.test_end")
    assert end["station_code"] in {"2A-T1", "2A-T2", "2A-T3", "2A-T4"}
    assert {"ocv_mv", "ir_mohm", "attempt"} <= end["payload"].keys()
    assert end["entity_id"].startswith("R0012-")
    box = next(e for e in ev if e["event_type"] == "box.closed")
    assert box["entity_id"][:7] in {"B0012-A", "B0012-B", "B0012-C"}
    # ACK = 마지막으로 처리한 순번
    assert plc.mem[mb.base + mb.read_seq] == seqs[-1]


def test_restart_resumes_without_duplicates(mb, tmp_path, clock):
    plc, ob, col = make(mb, tmp_path, clock)
    run_for(col, clock, 30)
    first = len(all_events(ob))
    # 수집기가 커밋은 했지만 ACK를 못 쓰고 죽은 경우: PLC ACK를 몇 칸 되돌림
    plc.write_word(mb.base + mb.read_seq, (col.last16 - 3) % 65536)
    col2 = Collector(mb, "2A", plc, Outbox(tmp_path / "ob.sqlite3"), 12, clock=lambda: T0)
    run_for(col2, clock, 30)
    ev = all_events(ob)
    seqs = [e["seq"] for e in ev]
    assert len(ev) > first
    assert seqs == list(range(1, len(ev) + 1))
    assert len(ev) == plc.written


def test_plc_overflow_when_collector_stops(mb, tmp_path, clock):
    plc, ob, col = make(mb, tmp_path, clock)
    run_for(col, clock, 5)
    clock.t += 60  # 수집기가 1분 멈춤 → PLC 링(8칸)이 차서 넘침
    col.poll_once()
    run_for(col, clock, 5)
    assert plc.dropped > 0
    assert col.c.plc_overflow == plc.dropped
    seqs = [e["seq"] for e in all_events(ob)]
    assert seqs == list(range(1, len(seqs) + 1))  # PLC가 버린 건 순번을 안 받으니 우리 쪽 순번은 연속


def test_lost_when_ack_disabled(mb, tmp_path, clock):
    from dataclasses import replace
    mb2 = replace(mb, ack=False)
    plc = FakePlc(mb2, clock=clock, cells_per_min=60)
    ob = Outbox(tmp_path / "ob.sqlite3")
    col = Collector(mb2, "2A", plc, ob, 1, clock=lambda: T0)
    col.poll_once()
    # ACK를 안 쓰면 PLC는 막히지 않음 → 링 쓰기를 직접 흉내 (가짜 PLC는 ACK 기준으로 막으므로 ACK를 따라 올림)
    for i in range(20):
        plc.push({"event": 1, "station": 1, "cell_no": 100 + i})
        plc.write_word(mb2.base + mb2.read_seq, plc.mem[mb2.base + mb2.write_seq])
    col.poll_once()
    assert col.c.lost > 0
    seqs = [e["seq"] for e in all_events(ob)]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_unknown_station_is_skipped_not_fatal(mb, tmp_path, clock):
    plc, ob, col = make(mb, tmp_path, clock, cells_per_min=0.0001)
    plc.push({"event": 1, "station": 99, "cell_no": 1})
    plc.push({"event": 1, "station": 1, "cell_no": 2})
    col.poll_once()
    ev = all_events(ob)
    assert [e["entity_id"] for e in ev] == ["R0012-000002"]
    assert col.c.unmapped == 1 and ob.stats()["dead"] == 1  # 버리지 않고 dead로 보관
    assert plc.mem[mb.base + mb.read_seq] == 2  # 그래도 ACK (PLC가 막히지 않게)


def _reset_test(mb, tmp_path, clock, reset_ack: bool):
    plc, ob, col = make(mb, tmp_path, clock, cells_per_min=0.0001)
    for i in range(5):
        plc.push({"event": 1, "station": 1, "cell_no": i + 1})
    col.poll_once()
    assert col.last16 == 5
    # 래더 재시작: 쓰기 순번 0 (reset_ack면 ACK도 0, 아니면 ACK는 5 그대로)
    plc.mem[mb.base + mb.write_seq] = 0
    if reset_ack:
        plc.mem[mb.base + mb.read_seq] = 0
    col.poll_once()
    assert plc.mem[mb.base + mb.read_seq] == 0  # PLC 쪽 순번에 맞춤 (5로 다시 쓰면 PLC가 넘침에 빠짐)
    for i in range(3):
        assert plc.push({"event": 1, "station": 1, "cell_no": 100 + i})
    col.poll_once()
    assert col.c.resyncs == 1 and col.c.lost == 0 and col.c.torn_skipped == 0 and plc.dropped == 0
    seqs = [e["seq"] for e in all_events(ob)]
    assert len(seqs) == 8 and seqs == sorted(seqs) and len(set(seqs)) == 8
    assert [e["entity_id"] for e in all_events(ob)][-3:] == ["R0012-000100", "R0012-000101", "R0012-000102"]


def test_plc_reset_early_in_run_both_zero(mb, tmp_path, clock):
    _reset_test(mb, tmp_path, clock, reset_ack=True)


def test_plc_reset_write_seq_only(mb, tmp_path, clock):
    _reset_test(mb, tmp_path, clock, reset_ack=False)


def test_unknown_event_code_kept_as_raw(mb, tmp_path, clock):
    plc, ob, col = make(mb, tmp_path, clock, cells_per_min=0.0001)
    plc.push({"event": 77, "station": 1, "cell_no": 5})
    col.poll_once()
    (e,) = all_events(ob)
    assert e["event_type"] == "plc.unknown_77"


def test_torn_record_gives_up_eventually(mb, tmp_path, clock):
    plc, ob, col = make(mb, tmp_path, clock, cells_per_min=0.0001)
    plc.push({"event": 1, "station": 1, "cell_no": 1})
    slot = mb.base + mb.record_base + (1 % mb.record_count) * mb.record_words
    plc.mem[slot] = 999  # 순번 칸이 계속 틀림 (PLC 버그)
    for _ in range(TORN_GIVE_UP):
        col.poll_once()
    assert col.c.torn_skipped == 1
    assert plc.mem[mb.base + mb.read_seq] == 1


def test_plc_reset_resyncs_and_keeps_seq_increasing(mb, tmp_path, clock):
    plc, ob, col = make(mb, tmp_path, clock)
    run_for(col, clock, 20)
    before = col.last_total
    # Factory I/O·래더 재시작: 메일박스가 0부터
    for off in (mb.write_seq, mb.read_seq):
        plc.mem[mb.base + off] = 0
    plc.mem[mb.base + mb.read_seq] = 500  # ACK가 엉뚱한 값 (재시작으로 초기화된 메모리)
    plc.mem[mb.base + mb.write_seq] = 500
    run_for(col, clock, 10)
    assert col.c.resyncs == 1
    seqs = [e["seq"] for e in all_events(ob)]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and seqs[-1] > before


def test_overflow_count_survives_plc_reset(mb, tmp_path, clock):
    plc, ob, col = make(mb, tmp_path, clock, cells_per_min=0.0001)
    ovf = mb.base + mb.overflow
    plc.mem[ovf] = 5  # PLC가 이미 센 값 (수집기 시작 전) → 세지 않음
    col.poll_once()
    plc.mem[ovf] = 7
    col.poll_once()
    assert col.c.plc_overflow == 2
    plc.mem[ovf] = 1  # PLC 재시작 후 다시 1
    col.poll_once()
    assert col.c.plc_overflow == 3


def test_health_snapshot(mb, tmp_path, clock):
    plc, ob, col = make(mb, tmp_path, clock)
    run_for(col, clock, 10)
    h = col.health()
    assert h["line"] == "2A" and h["run_id"] == 12 and h["events"] == col.c.events > 0
    assert h["outbox_pending"] == col.c.events and h["detail"]["last_seq"] == col.last_total


def test_resend_dead_after_config_fix(mb, tmp_path, clock):
    from dataclasses import replace
    from battery_collector import __main__ as cli
    import argparse
    plc, ob, col = make(mb, tmp_path, clock, cells_per_min=0.0001)
    plc.push({"event": 2, "station": 99, "cell_no": 7})
    col.poll_once()
    assert ob.stats()["dead"] == 1
    fixed_mb = replace(mb, stations={**mb.stations, 99: "2A-IN"})

    class Cfg:
        mailbox, line, outbox_path = fixed_mb, "2A", tmp_path / "ob.sqlite3"
    cli.cmd_resend_dead(Cfg, argparse.Namespace(dry_run=False, no_upload=True))
    s = ob.stats()
    assert (s["dead"], s["pending"]) == (0, 1)
    (e,) = ob.next_batch(10)
    assert e["station_code"] == "2A-IN" and e["entity_id"] == "R0012-000007" and e["seq"] == 1


def test_overflow_while_collector_was_down_is_counted(mb, tmp_path, clock):
    plc, ob, col = make(mb, tmp_path, clock, cells_per_min=0.0001)
    col.poll_once()  # 시작: 넘침 0 기억
    plc.mem[mb.base + mb.overflow] = 50  # 수집기가 꺼진 동안 PLC가 50건 버림
    col2 = Collector(mb, "2A", plc, Outbox(tmp_path / "ob.sqlite3"), 12, clock=lambda: T0)
    col2.poll_once()
    assert col2.c.plc_overflow == 50
