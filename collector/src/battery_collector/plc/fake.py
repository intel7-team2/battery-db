"""가짜 PLC. 래더가 할 일을 흉내 내서 메일박스에 셀 이벤트를 쓴다.

- 스레드 없이, read_words가 불릴 때 그 시각까지 생길 이벤트를 메일박스에 채운다.
- 메일박스 규칙을 그대로 지킨다: 레코드를 먼저 쓰고 write_seq를 올림, 가득 차면 overflow +1 하고 버림.
- 전압·저항 값은 interface.md 가정값 기준 (OCV 3500~3800 mV, 정상 R 20~45 mΩ, 불량 70 mΩ 이상).
  실제 분포가 정해지면 바꾼다.
"""
from __future__ import annotations

import heapq
import random
import time
from typing import Callable

from ..config import Mailbox

WRAP = 1 << 16

# 2a.toml의 코드와 같아야 한다 (가짜라서 여기 고정)
EV = {"created": 1, "id_read": 2, "appearance": 3, "scrapped": 4, "test_start": 5, "test_end": 6,
      "id_check": 7, "sorted": 8, "packed": 9, "box_closed": 10, "handoff": 11}
ST = {"IN": 1, "VIS1": 2, "VIS2": 3, "RJ1": 4, "T": 11, "IDC": 20, "SORT": 21, "LANE": 31, "RJ2": 41,
      "BOX": 50, "OUT": 51}
GRADE = {1: "A", 2: "B", 3: "C"}


class FakePlc:
    def __init__(self, mb: Mailbox, cells_per_min: float = 30, appearance_ng: float = 0.05,
                 test_ng: float = 0.08, seed: int | None = 7, clock: Callable[[], float] = time.monotonic,
                 lot: int = 1):
        self.mb = mb
        self.mem: dict[int, int] = {}
        self.clock = clock
        self.rng = random.Random(seed)
        self.interval = 60.0 / cells_per_min
        self.appearance_ng = appearance_ng
        self.test_ng = test_ng
        self.lot = lot
        self.queue: list[tuple[float, int, dict]] = []  # (시각, 순서, 레코드)
        self._order = 0
        self._next_cell_at = clock() + self.interval
        self._cell_no = 0
        self._lane = 0
        self._boxes: dict[int, list[int]] = {1: [], 2: [], 3: []}  # 등급 → OCV 목록
        self._box_no = 0
        self._box_for: dict[int, int] = {}  # 등급 → 지금 채우는 박스 번호
        self.dropped = 0
        self.written = 0
        self.closed = False

    # ── PlcPort ──
    def read_words(self, d_start: int, count: int) -> list[int]:
        self._advance(self.clock())
        return [self.mem.get(d_start + i, 0) for i in range(count)]

    def write_word(self, d_addr: int, value: int) -> None:
        self.mem[d_addr] = value & 0xFFFF

    def close(self) -> None:
        self.closed = True

    # ── 메일박스 쓰기 (래더가 할 일) ──
    def _d(self, off: int) -> int:
        return self.mb.base + off

    def push(self, rec: dict) -> bool:
        """레코드 1개 쓰기. 가득 차면 False (넘침)."""
        mb = self.mb
        w = self.mem.get(self._d(mb.write_seq), 0)
        r = self.mem.get(self._d(mb.read_seq), 0)
        if (w - r) % WRAP >= mb.record_count:
            self.mem[self._d(mb.overflow)] = (self.mem.get(self._d(mb.overflow), 0) + 1) & 0xFFFF
            self.dropped += 1
            return False
        s = (w + 1) % WRAP
        start = self._d(mb.record_base) + (s % mb.record_count) * mb.record_words
        for i in range(mb.record_words):
            self.mem[start + i] = 0
        rec = {**rec, "seq": s}
        for name, spec in mb.fields.items():
            v = int(rec.get(name, 0))
            self.mem[start + spec.offset] = v & 0xFFFF
            if spec.words == 2:
                self.mem[start + spec.offset + 1] = (v >> 16) & 0xFFFF
        self.mem[self._d(mb.write_seq)] = s  # 레코드를 다 쓴 뒤에 순번을 올린다
        self.written += 1
        return True

    # ── 가짜 공정 ──
    def _at(self, t: float, **rec) -> None:
        # 실제 라인처럼 조금씩 어긋나게 (같은 순간에 8건 넘게 몰리면 메일박스가 넘친다)
        self._order += 1
        heapq.heappush(self.queue, (t + self.rng.uniform(0, 0.9), self._order, rec))

    def _new_cell(self, t: float) -> None:
        self._cell_no += 1
        n, lot, rng = self._cell_no, self.lot, self.rng
        base = {"cell_no": n, "lot": lot}
        self._at(t, event=EV["created"], station=ST["IN"], **base)
        self._at(t + 1.0, event=EV["id_read"], station=ST["VIS1"], **base)
        if rng.random() < self.appearance_ng:
            self._at(t + 2.0, event=EV["appearance"], station=ST["VIS2"], result=9, reason=7, **base)
            self._at(t + 3.0, event=EV["scrapped"], station=ST["RJ1"], result=9, reason=7, **base)
            return
        self._at(t + 2.0, event=EV["appearance"], station=ST["VIS2"], result=0, **base)
        lane = self._lane % 4 + 1
        self._lane += 1
        tester = ST["T"] + lane - 1
        ocv = int(rng.gauss(3700, 60))
        bad = rng.random() < self.test_ng
        ir = rng.randint(70, 90) if bad else rng.randint(20, 45)
        if bad:
            grade, reason = 9, 3
        elif ocv < 3500:
            grade, reason = 9, 1
        elif ocv > 3800:
            grade, reason = 9, 2
        else:
            grade, reason = (1 if ocv >= 3750 else 2 if ocv >= 3650 else 3), 0
        self._at(t + 4.0, event=EV["test_start"], station=tester, lane=lane, value1=1, **base)
        self._at(t + 20.0, event=EV["test_end"], station=tester, lane=lane, result=grade, reason=reason,
                 value1=ocv, value2=ir, value3=1, **base)
        if grade == 9:
            self._at(t + 30.0, event=EV["scrapped"], station=ST["RJ2"], result=9, reason=reason, **base)
            return
        self._at(t + 26.0, event=EV["id_check"], station=ST["IDC"], result=0, **base)
        self._at(t + 28.0, event=EV["sorted"], station=ST["LANE"] + grade - 1, result=grade, **base)
        self._at(t + 35.0, event=-1, grade=grade, ocv=ocv, **base)  # 박스 적재 (아래 _box에서 풀어 씀)

    def _box(self, t: float, rec: dict) -> None:
        g, ocvs = rec["grade"], self._boxes[rec["grade"]]
        if not ocvs:
            self._box_no += 1
            self._box_for[g] = self._box_no
        box_no = self._box_for[g]
        ocvs.append(rec["ocv"])
        self.push(dict(event=EV["packed"], station=ST["BOX"], cell_no=rec["cell_no"], lot=rec["lot"],
                       result=g, value1=box_no, value2=len(ocvs)))
        if len(ocvs) == 4:
            spread = max(ocvs) - min(ocvs)
            self._at(t + 2.0, event=EV["box_closed"], station=ST["BOX"], cell_no=box_no, result=g, value1=spread)
            self._at(t + 6.0, event=EV["handoff"], station=ST["OUT"], cell_no=box_no, result=g)
            self._boxes[g] = []

    def _advance(self, now: float) -> None:
        while self._next_cell_at <= now:
            self._new_cell(self._next_cell_at)
            self._next_cell_at += self.interval
        while self.queue and self.queue[0][0] <= now:
            t, _, rec = heapq.heappop(self.queue)
            if rec["event"] == -1:
                self._box(t, rec)
            else:
                self.push(rec)
