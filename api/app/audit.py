"""접근 로그: 요청 1건 = api_access_log 1행 (누가, 무엇을, 결과, 걸린 시간).

응답을 늦추지 않도록 큐에 넣고 2초마다 묶어서 쓴다. DB에 못 쓰면 버리고 로그만 남긴다.
journald에도 한 줄씩 남는다: journalctl -u battery-api
"""
import asyncio
import datetime as dt
import logging
import sys
import time

from fastapi import Request

from . import db

SKIP = {"/health", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json", "/favicon.ico"}

log = logging.getLogger("battery-api.access")
if not log.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False

_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)


async def middleware(request: Request, call_next):
    at = dt.datetime.now(dt.timezone.utc)
    t0 = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        path = request.url.path
        if path not in SKIP:
            client = getattr(request.state, "client_name", None)
            ms = int((time.perf_counter() - t0) * 1000)
            log.info("%s %s %s %d %dms", client or "-", request.method, path, status, ms)
            try:
                _queue.put_nowait((at, client, request.method, path[:300], status, ms))
            except asyncio.QueueFull:
                pass


async def _flush(batch: list[tuple]) -> None:
    try:
        cols = list(zip(*batch))
        async with (await db.pool("api_audit")).acquire() as conn:
            await conn.execute(
                """INSERT INTO api_access_log (at, client, method, path, status, ms)
                   SELECT * FROM unnest($1::timestamptz[], $2::text[], $3::text[], $4::text[],
                                        $5::smallint[], $6::int[])""",
                *[list(c) for c in cols])
    except Exception as e:  # 접근 로그 때문에 API가 멈추면 안 된다
        log.warning("access log write failed (%d rows dropped): %s", len(batch), e)


async def writer() -> None:
    """lifespan에서 띄우는 백그라운드 작업."""
    try:
        while True:
            batch = [await _queue.get()]
            await asyncio.sleep(2)
            while not _queue.empty() and len(batch) < 1000:
                batch.append(_queue.get_nowait())
            await _flush(batch)
    except asyncio.CancelledError:
        rest = []
        while not _queue.empty():
            rest.append(_queue.get_nowait())
        if rest:
            await _flush(rest)
        raise
