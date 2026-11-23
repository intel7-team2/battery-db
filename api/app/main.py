"""battery-db API. 팀원은 DB 계정 대신 API 키로 읽고 쓴다.

    uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
문서: /docs (Swagger), /openapi.json
"""
import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from . import audit, config, db, routes_read, routes_write
from .auth import Client, client

VERSION = "0.2.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(audit.writer())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await db.close_all()


app = FastAPI(
    title="battery-db API", version=VERSION, lifespan=lifespan,
    description="2A·팩 라인 공용 DB. 인증: `X-API-Key: bdb_...` 또는 `Authorization: Bearer bdb_...`. "
                "단위: 전압 mV, 전류 mA, 내부저항 mΩ, 시각 UTC. "
                "키 발급: `ssh tunnel@<서버> new` (GitHub 키 등록된 팀원).",
)
app.middleware("http")(audit.middleware)
if config.CORS_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS,
                       allow_methods=["GET", "POST"], allow_headers=["X-API-Key", "Authorization", "Content-Type"])


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/docs")


@app.get("/health", tags=["meta"])
async def health():
    """인증 없음. DB까지 닿는지만 확인."""
    r = await db.fetchrow("SELECT current_database() AS db, now() AS db_time")
    return {"ok": True, "version": VERSION, **r}


@app.get("/me", tags=["meta"])
async def me(c: Client = Depends(client)):
    """내 키 정보와 사용량 (v_api_usage). 어떤 scope든 유효한 키면 된다."""
    usage = await db.fetchrow("SELECT * FROM v_api_usage WHERE client = $1", c.name)
    return {"name": c.name, "scopes": sorted(c.scopes), "key_prefix": c.prefix,
            "created_at": c.created_at, "issued_by": c.source, "usage": usage}


app.include_router(routes_read.router)
app.include_router(routes_write.router)
