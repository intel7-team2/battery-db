"""PLC 이벤트 메일박스(링 버퍼) 해석. PLC와 통신하지 않는 순수 로직이라 단위 시험이 쉽다.

규칙 (2a-data-design.md 초안):
- PLC는 레코드를 다 쓴 **다음에** 쓰기 순번(write_seq)을 올린다. 순번은 16비트라 65535 다음 0.
- 순번 s의 레코드는 s % record_count 번째 자리에 있고, 레코드 첫 워드에도 s가 들어 있다.
- 수집기는 처리한 순번을 read_seq에 쓴다 (ACK). PLC는 write_seq - read_seq 가 record_count면 넘침.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Mailbox

WRAP = 1 << 16


@dataclass(frozen=True)
class Header:
    write_seq: int
    read_seq: int
    overflow: int


@dataclass(frozen=True)
class Record:
    seq: int                    # 16비트 원래 값
    fields: dict[str, int]


@dataclass(frozen=True)
class ReadResult:
    header: Header
    records: list[Record]       # 순번 순서, 새로 읽을 것만
    lost: int                   # 덮어써져서 못 읽은 레코드 수
    torn: list[int]             # 순번이 안 맞은 자리 (PLC가 쓰는 중이었거나 덮어씀)


def _word(words: list[int], i: int) -> int:
    return words[i] & 0xFFFF


def _value(words: list[int], start: int, words_n: int) -> int:
    if words_n == 1:
        return _word(words, start)
    return _word(words, start) | (_word(words, start + 1) << 16)


def header(mb: Mailbox, words: list[int]) -> Header:
    return Header(_word(words, mb.write_seq), _word(words, mb.read_seq), _word(words, mb.overflow))


def decode_record(mb: Mailbox, words: list[int], slot: int) -> Record:
    start = mb.record_base + slot * mb.record_words
    f = {name: _value(words, start + spec.offset, spec.words) for name, spec in mb.fields.items()}
    return Record(seq=f["seq"], fields=f)


def pending(after: int, write_seq: int) -> int:
    """after 다음부터 write_seq까지 몇 개 (16비트 순환)."""
    return (write_seq - after) % WRAP


def read_new(mb: Mailbox, words: list[int], after: int) -> ReadResult:
    """after(마지막으로 처리한 16비트 순번) 이후의 레코드를 순서대로 꺼낸다."""
    if len(words) < mb.size:
        raise ValueError(f"need {mb.size} words, got {len(words)}")
    h = header(mb, words)
    n = pending(after, h.write_seq)
    lost = max(0, n - mb.record_count)
    first = (after + 1 + lost) % WRAP
    records, torn = [], []
    for i in range(n - lost):
        s = (first + i) % WRAP
        rec = decode_record(mb, words, s % mb.record_count)
        if rec.seq != s:
            torn.append(s)
            break  # 그 뒤는 아직 안 쓰였을 수 있으니 다음 폴링에서
        records.append(rec)
    return ReadResult(h, records, lost, torn)


def unwrap(prev_total: int, seq16: int) -> int:
    """16비트 순번 → 이 run 안에서 계속 증가하는 순번. event_id(run, seq)가 겹치지 않게."""
    return prev_total + ((seq16 - prev_total) % WRAP)
