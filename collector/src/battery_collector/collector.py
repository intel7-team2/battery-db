"""폴링 루프: PLC 메일박스 읽기 → 이벤트 변환 → outbox 커밋 → ACK.

순서가 중요하다. **outbox에 커밋한 다음에 ACK를 쓴다.**
- 커밋 전에 죽으면: ACK가 안 갔으니 PLC에 레코드가 남아 다시 읽는다.
- 커밋 후 ACK 전에 죽으면: 다시 읽지만 event_id가 같아 outbox·DB에서 한 번만 남는다.
그래서 이벤트는 "적어도 한 번" 전달되고, (run_id, seq) 중복 제거로 결과는 정확히 한 번이 된다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import mailbox as mbx
from .config import Mailbox
from .events import UnknownCode, to_api_event
from .outbox import Outbox
from .plc.base import PlcPort

log = logging.getLogger("collector.poll")

TORN_GIVE_UP = 20  # 같은 자리가 이만큼 연속으로 안 맞으면 PLC 쪽 문제로 보고 건너뜀


@dataclass
class Counters:
    polls: int = 0
    events: int = 0
    lost: int = 0
    unmapped: int = 0
    resyncs: int = 0
    plc_overflow: int = 0
    torn_skipped: int = 0
    last_poll: str | None = None
    errors: list[str] = field(default_factory=list)


class Collector:
    def __init__(self, mb: Mailbox, line: str, port: PlcPort, outbox: Outbox, run_id: int,
                 clock=lambda: datetime.now(timezone.utc)):
        self.mb, self.line, self.port, self.outbox, self.run_id = mb, line, port, outbox, run_id
        self.clock = clock
        self.c = Counters()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._key = f"run:{run_id}:last_seq"
        saved = outbox.get_state(self._key)
        self.last_total: int | None = int(saved) if saved is not None else None
        self._torn_seq: int | None = None
        self._torn_count = 0
        # 수집기가 꺼져 있던 동안의 PLC 넘침도 세려고 마지막으로 본 D2702 값을 outbox에 둔다
        self._ovf_key = f"run:{run_id}:overflow_raw"
        saved_ovf = outbox.get_state(self._ovf_key)
        self._overflow_raw: int | None = int(saved_ovf) if saved_ovf is not None else None
        outbox.set_state("run_id", str(run_id))

    def health(self) -> dict:
        """POST /collector/health 본문 (outbox 수 포함). 업로드 스레드가 30초마다 보낸다."""
        s = self.outbox.stats()
        return {"line": self.line, "run_id": self.run_id, "started_at": self.started_at,
                "last_poll_at": self.c.last_poll, "polls": self.c.polls, "events": self.c.events,
                "lost": self.c.lost, "torn_skipped": self.c.torn_skipped, "unmapped": self.c.unmapped,
                "resyncs": self.c.resyncs, "plc_overflow": self.c.plc_overflow,
                "outbox_pending": s["pending"], "outbox_dead": s["dead"],
                "detail": {"last_seq": self.last_total, "last_error": s["last_error"]}}

    @property
    def last16(self) -> int:
        return (self.last_total or 0) % mbx.WRAP

    def _ack_addr(self) -> int:
        return self.mb.base + self.mb.read_seq

    def _start_point(self, h: mbx.Header) -> int:
        """PLC 헤더만 보고 어디부터 읽을지. ACK가 쓰기 순번 뒤 링 안에 있으면 거기부터, 아니면 지금부터."""
        if self.mb.ack and mbx.pending(h.read_seq, h.write_seq) <= self.mb.record_count:
            return h.read_seq
        return h.write_seq

    def _window(self) -> int:
        # ACK 모드에서 PLC는 우리 ACK보다 링 크기 넘게 앞설 수 없다. ACK가 없으면 늦어서 앞설 수 있으니 반 바퀴까지
        return self.mb.record_count if self.mb.ack else mbx.WRAP // 2

    def _sync(self, h: mbx.Header) -> None:
        """처음 시작, PLC 재시작, ACK 유실을 정리한다."""
        if self.last_total is None:
            start = self._start_point(h)
            self.last_total = start
            log.info("run %s: start at PLC seq %d (write_seq %d, ACK %d)",
                     self.run_id, start, h.write_seq, h.read_seq)
            if self.mb.ack and h.read_seq != start:
                self.port.write_word(self._ack_addr(), start)
            return

        if mbx.pending(self.last16, h.write_seq) > self._window():
            # PLC 쓰기 순번이 우리 위치에서 말이 안 되게 떨어져 있음 = PLC·래더 재시작으로 메일박스가 초기화됨.
            # 같은 run을 계속 쓰면 seq는 계속 늘려서 event_id가 겹치지 않게 한다.
            start = self._start_point(h)
            self.c.resyncs += 1
            log.warning("PLC mailbox reset (write %d, ACK %d, ours %d): resync from %d. "
                        "Factory I/O를 다시 시작했다면 새 run을 여는 게 맞다",
                        h.write_seq, h.read_seq, self.last16, start)
            self.last_total = mbx.unwrap(self.last_total + 1, start) if start != self.last16 else self.last_total
            if self.mb.ack and h.read_seq != start:
                self.port.write_word(self._ack_addr(), start)
            return

        if self.mb.ack and h.read_seq != self.last16:
            # 쓰기 순번은 정상 범위. ACK만 다름 = 지난 ACK 쓰기가 안 감(또는 누가 덮어씀). 우리 값으로 다시 씀
            log.warning("ACK mismatch (PLC %d, ours %d), rewriting", h.read_seq, self.last16)
            self.port.write_word(self._ack_addr(), self.last16)

    def poll_once(self) -> int:
        """한 번 읽고 처리. 새로 outbox에 넣은 이벤트 수를 돌려준다."""
        words = self.port.read_words(self.mb.base, self.mb.size)
        now = self.clock()
        self.c.polls += 1
        self.c.last_poll = now.isoformat()
        h = mbx.header(self.mb, words)
        # PLC 넘침 수(D2702)는 PLC가 센 누적값. 수집기 시작 뒤 늘어난 만큼만 더한다 (PLC 재시작으로 0이 되면 거기서 다시)
        if self._overflow_raw is not None and h.overflow != self._overflow_raw:
            d = (h.overflow - self._overflow_raw) % mbx.WRAP
            if d > mbx.WRAP // 2:  # 줄어듦 = PLC 재시작
                d = h.overflow
            if d:
                self.c.plc_overflow += d
                log.error("PLC mailbox overflow: %d more events dropped in PLC (total %d)", d, self.c.plc_overflow)
        if h.overflow != self._overflow_raw:
            self._overflow_raw = h.overflow
            self.outbox.set_state(self._ovf_key, str(h.overflow))
        self._sync(h)

        res = mbx.read_new(self.mb, words, self.last16)
        total = self.last_total
        if res.lost:
            self.c.lost += res.lost
            log.error("lost %d records (collector too slow or ACK off); skipping to seq %d",
                      res.lost, (self.last16 + res.lost + 1) % mbx.WRAP)
            total += res.lost

        if res.torn:
            s = res.torn[0]
            self._torn_count = self._torn_count + 1 if s == self._torn_seq else 1
            self._torn_seq = s
            if self._torn_count >= TORN_GIVE_UP and not res.records:
                log.error("record seq %d never became consistent, skipping it", s)
                self.c.torn_skipped += 1
                total = mbx.unwrap(total, s)
                self._torn_seq, self._torn_count = None, 0
        else:
            self._torn_seq, self._torn_count = None, 0

        events, dead = [], []
        for rec in res.records:
            total = mbx.unwrap(total, rec.seq)
            try:
                events.append(to_api_event(self.mb, self.line, self.run_id, total, rec, now))
            except UnknownCode as e:
                # 버리지 않고 outbox에 dead로 남긴다 (설정을 고친 뒤 원본에서 복구할 수 있게)
                self.c.unmapped += 1
                dead.append((e.event, str(e)))
                log.error("unmapped record kept as dead: %s (fields %s)", e, rec.fields)

        if total == self.last_total:
            return 0
        added = self.outbox.add(events, {self._key: str(total)}, dead=dead)
        self.last_total = total
        self.c.events += added
        if self.mb.ack:
            self.port.write_word(self._ack_addr(), self.last16)
        return added
