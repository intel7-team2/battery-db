"""battery-db API. 팀원은 DB 계정 대신 API 키로 읽고 쓴다.

    uvicorn app.main:app --host 127.0.0.1 --port 8000
문서: /docs (Swagger), /openapi.json
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import config, db, routes_read, routes_write

VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await db.close_all()


app = FastAPI(
    title="battery-db API", version=VERSION, lifespan=lifespan,
    description="2A·팩 라인 공용 DB. 인증: `X-API-Key: bdb_...` 또는 `Authorization: Bearer bdb_...`. "
                "단위: 전압 mV, 전류 mA, 내부저항 mΩ, 시각 UTC.",
)
if config.CORS_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS,
                       allow_methods=["GET", "POST"], allow_headers=["X-API-Key", "Authorization", "Content-Type"])


@app.get("/health", tags=["meta"])
async def health():
    """인증 없음. DB까지 닿는지만 확인."""
    r = await db.fetchrow("SELECT current_database() AS db, now() AS db_time")
    return {"ok": True, "version": VERSION, **r}


app.include_router(routes_read.router)
app.include_router(routes_write.router)
