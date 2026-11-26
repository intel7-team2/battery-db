"""메일박스 레코드 → API POST /events 한 건.

event_id 규칙은 API(api/app/routes_write.py)와 똑같아야 한다:
    uuid5(uuid5(NAMESPACE_URL, "https://github.com/intel7-team2/battery-db/event"), "run:<run_id>/seq:<seq>")
같은 (run, seq)는 몇 번 보내도 DB에 한 번만 들어간다.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from .config import Mailbox
from .mailbox import Record

EVENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/intel7-team2/battery-db/event")


def event_uuid(run_id: int, seq: int) -> uuid.UUID:
    return uuid.uuid5(EVENT_NAMESPACE, f"run:{run_id}/seq:{seq}")


class UnknownCode(ValueError):
    """설정에 없는 스테이션 번호. API가 받지 않으므로 보내지 않고, .event(원본 포함)를 outbox에 dead로 남긴다."""

    def __init__(self, msg: str, event: dict):
        super().__init__(msg)
        self.event = event


def entity_id(kind: str, run_id: int, fields: dict[str, int], grades: dict[int, str]) -> str | None:
    no = fields.get("cell_no", 0)
    if kind == "cell":
        return f"R{run_id:04d}-{no:06d}" if no else None
    if kind == "box":
        grade = grades.get(fields.get("result", 0), "X")
        return f"B{run_id:04d}-{grade}{no:03d}" if no else None
    return None


def to_api_event(mb: Mailbox, line: str, run_id: int, seq: int, rec: Record,
                 occurred_at: datetime) -> dict:
    """seq는 이 run 안에서 계속 증가하는 순번 (mailbox.unwrap 결과)."""
    f = rec.fields
    code, st = f["event"], f["station"]
    ev = mb.events.get(code)
    station = mb.stations.get(st)

    payload: dict = {"plc": {k: v for k, v in f.items() if k != "seq"}, "seq16": rec.seq}
    for k in ("lane", "lot", "result", "reason"):
        if k in f:
            payload[k] = f[k]
    if ev is None:
        event_type, kind = f"plc.unknown_{code}", "cell"
    else:
        event_type, kind = ev.type, ev.entity
        for i, name in enumerate(ev.values, start=1):
            if f"value{i}" in f:
                payload[name] = f[f"value{i}"]

    out = {
        "event_id": str(event_uuid(run_id, seq)),
        "run_id": run_id,
        "seq": seq,
        "source": line,
        "station_code": station or f"PLC-ST{st}",
        "event_type": event_type,
        "entity_id": entity_id(kind, run_id, f, mb.grades),
        "payload": payload,
        # PLC 레코드에 시각이 없어서 수집기가 읽은 시각 (폴링 주기만큼 늦을 수 있음)
        "occurred_at": occurred_at.isoformat(),
    }
    if station is None:
        raise UnknownCode(f"station number {st} not in config (seq {seq})", out)
    return out
