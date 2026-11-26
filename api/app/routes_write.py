"""쓰기 엔드포인트 (scope write:2a / write:pack). 초안.

지금은 원본 이벤트(event_log)와 시험 1회(sim_run)만 쓴다.
이벤트를 cell·cell_inspection 등으로 옮기는 규칙은 PLC 메일박스·ACK 합의 뒤에 붙인다.

중복 방지: 같은 event_id는 한 번만 들어간다. 재전송해도 안전하다.
event_id를 안 주면 run_id + seq(PLC 메일박스 순번)로 만든다:
    uuid5(EVENT_NAMESPACE, f"run:{run_id}/seq:{seq}")
"""
import uuid
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from . import db
from .auth import Client, writer

router = APIRouter(tags=["write"])

EVENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/intel7-team2/battery-db/event")


def event_uuid(run_id: int, seq: int) -> uuid.UUID:
    return uuid.uuid5(EVENT_NAMESPACE, f"run:{run_id}/seq:{seq}")


def _check_line(c: Client, line: str) -> str:
    if line not in c.lines:
        raise HTTPException(403, f"this key cannot write line '{line}'")
    return db.WRITER_FOR_LINE[line]


# ── 시험 1회 ─────────────────────────────────────────────

class RunIn(BaseModel):
    line: Literal["2A", "PACK"]
    purpose: str = Field("demo", max_length=32)
    scene_version: str | None = Field(None, max_length=64)
    ladder_version: str | None = Field(None, max_length=64)
    note: str | None = Field(None, max_length=500)


class RunEnd(BaseModel):
    note: str | None = Field(None, max_length=500)


@router.post("/runs", status_code=201)
async def start_run(body: RunIn, c: Client = Depends(writer)):
    """시험 시작. 돌려받은 run_id를 이후 이벤트에 붙인다 (Factory I/O 재시작마다 새 run)."""
    role = _check_line(c, body.line)
    return await db.fetchrow(
        """INSERT INTO sim_run (line, purpose, scene_version, ladder_version, note, api_client)
           VALUES ($1, $2, $3, $4, $5, $6) RETURNING run_id, line, purpose, started_at, api_client""",
        body.line, body.purpose, body.scene_version, body.ladder_version, body.note, c.name, role=role)


@router.post("/runs/{run_id}/end")
async def end_run(run_id: int, body: RunEnd | None = None, c: Client = Depends(writer)):
    r = await db.fetchrow("SELECT line, ended_at FROM sim_run WHERE run_id = $1", run_id)
    if r is None:
        raise HTTPException(404, "run not found")
    role = _check_line(c, r["line"])
    if r["ended_at"] is not None:
        raise HTTPException(409, "run already ended")
    return await db.fetchrow(
        """UPDATE sim_run SET ended_at = now(), note = coalesce($2, note)
           WHERE run_id = $1 AND ended_at IS NULL RETURNING run_id, started_at, ended_at, note""",
        run_id, body.note if body else None, role=role)


# ── 이벤트 ───────────────────────────────────────────────

class EventIn(BaseModel):
    event_id: uuid.UUID | None = None
    run_id: int | None = None
    seq: int | None = Field(None, ge=0, description="PLC 메일박스 순번. event_id 대신 run_id와 함께")
    source: Literal["2A", "PACK"]
    station_code: str = Field(max_length=32)
    event_type: str = Field(min_length=1, max_length=64, description="예: cell.inspected, box.full")
    entity_id: str | None = Field(None, max_length=64, description="대상 셀·박스·팩 ID")
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: AwareDatetime = Field(description="현장 시각, 시간대 포함 (예: 2026-11-23T14:03:05+09:00)")

    @model_validator(mode="after")
    def _id(self):
        if self.event_id is None:
            if self.run_id is None or self.seq is None:
                raise ValueError("give event_id, or run_id + seq")
            self.event_id = event_uuid(self.run_id, self.seq)
        return self


class EventBatch(BaseModel):
    events: list[EventIn] = Field(min_length=1, max_length=500)


@router.post("/events")
async def post_events(body: EventBatch, c: Client = Depends(writer)):
    """이벤트 묶음 저장 (최대 500건, 한 라인만). 전부 들어가거나 전부 거절된다.

    응답의 duplicates는 이미 있던 event_id 수. 재전송이면 정상이다.
    """
    sources = {e.source for e in body.events}
    if len(sources) != 1:
        raise HTTPException(422, "one batch = one source line")
    line = sources.pop()
    role = _check_line(c, line)

    codes = sorted({e.station_code for e in body.events})
    known = {r["station_code"]: r["line"] for r in await db.fetch(
        "SELECT station_code, line FROM station WHERE station_code = ANY($1::text[])", codes)}
    unknown = [s for s in codes if s not in known]
    wrong = [s for s in codes if s in known and known[s] != line]
    if unknown or wrong:
        raise HTTPException(422, {"unknown_stations": unknown, f"not_{line}_stations": wrong})

    ev = body.events
    try:
        inserted = await db.fetch(
            """INSERT INTO event_log (event_id, source, station_code, event_type, entity_id,
                                      run_id, payload, occurred_at, seq, api_client)
               SELECT u.*, $10 FROM unnest($1::uuid[], $2::text[], $3::text[], $4::text[], $5::text[],
                                           $6::int[], $7::jsonb[], $8::timestamptz[], $9::bigint[]) AS u
               ON CONFLICT (event_id) DO NOTHING
               RETURNING event_id""",
            [e.event_id for e in ev], [e.source for e in ev], [e.station_code for e in ev],
            [e.event_type for e in ev], [e.entity_id for e in ev], [e.run_id for e in ev],
            [e.payload for e in ev], [e.occurred_at for e in ev], [e.seq for e in ev], c.name,
            role=role)
    except asyncpg.ForeignKeyViolationError as err:
        raise HTTPException(422, f"unknown reference: {err.detail}") from err

    unique = len({e.event_id for e in ev})
    return {"received": len(ev), "inserted": len(inserted), "duplicates": unique - len(inserted),
            "event_ids": [str(e.event_id) for e in ev]}


# ── 수집기 상태 (schema/06_collector.sql) ────────────────

class CollectorHealthIn(BaseModel):
    line: Literal["2A", "PACK"]
    run_id: int | None = None
    started_at: AwareDatetime | None = None
    last_poll_at: AwareDatetime | None = None
    polls: int = Field(0, ge=0)
    events: int = Field(0, ge=0)
    lost: int = Field(0, ge=0)
    torn_skipped: int = Field(0, ge=0)
    unmapped: int = Field(0, ge=0)
    resyncs: int = Field(0, ge=0)
    plc_overflow: int = Field(0, ge=0)
    outbox_pending: int = Field(0, ge=0)
    outbox_dead: int = Field(0, ge=0)
    upload_backoff_s: float | None = Field(None, ge=0)
    detail: dict[str, Any] = Field(default_factory=dict)


@router.post("/collector/health", status_code=201)
async def collector_health(body: CollectorHealthIn, c: Client = Depends(writer)):
    """수집기 상태 1건 (30초마다). 누락 수는 수집기 프로세스 시작부터 누적. 조회는 GET /collectors."""
    role = _check_line(c, body.line)
    try:
        return await db.fetchrow(
            """INSERT INTO collector_health (line, run_id, api_client, started_at, last_poll_at, polls, events,
                                             lost, torn_skipped, unmapped, resyncs, plc_overflow,
                                             outbox_pending, outbox_dead, upload_backoff_s, detail)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
               RETURNING id, reported_at""",
            body.line, body.run_id, c.name, body.started_at, body.last_poll_at, body.polls, body.events,
            body.lost, body.torn_skipped, body.unmapped, body.resyncs, body.plc_overflow,
            body.outbox_pending, body.outbox_dead, body.upload_backoff_s, body.detail, role=role)
    except asyncpg.ForeignKeyViolationError as err:
        raise HTTPException(422, f"unknown reference: {err.detail}") from err
