"""DB 연결. DB 역할마다 연결 풀을 따로 둬서 04_roles.sql의 권한 분리를 그대로 쓴다.

    reader      조회
    writer_2a   2A 쓰기 (API 키 scope write:2a)
    writer_pack 팩 쓰기 (API 키 scope write:pack)
"""
import asyncio
import json

import asyncpg

from . import config

ROLES = ("reader", "writer_2a", "writer_pack")
WRITER_FOR_LINE = {"2A": "writer_2a", "PACK": "writer_pack"}

_pools: dict[str, asyncpg.Pool] = {}
_lock = asyncio.Lock()


async def _init(conn: asyncpg.Connection) -> None:
    for t in ("json", "jsonb"):
        await conn.set_type_codec(t, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def pool(role: str = "reader") -> asyncpg.Pool:
    if role not in ROLES:
        raise ValueError(role)
    p = _pools.get(role)
    if p is None:
        async with _lock:
            p = _pools.get(role)
            if p is None:
                # 1 GB VM: max_connections=30 중 API가 쓰는 건 최대 8개
                p = await asyncpg.create_pool(
                    host=config.DB_HOST, port=config.DB_PORT, database=config.DB_NAME,
                    user=role, password=config.db_password(role),
                    min_size=1 if role == "reader" else 0,
                    max_size=4 if role == "reader" else 2,
                    command_timeout=15, init=_init,
                    server_settings={"application_name": "battery-api"},
                )
                _pools[role] = p
    return p


async def fetch(sql: str, *args, role: str = "reader") -> list[dict]:
    async with (await pool(role)).acquire() as conn:
        return [dict(r) for r in await conn.fetch(sql, *args)]


async def fetchrow(sql: str, *args, role: str = "reader") -> dict | None:
    async with (await pool(role)).acquire() as conn:
        r = await conn.fetchrow(sql, *args)
        return dict(r) if r else None


async def close_all() -> None:
    for p in _pools.values():
        await p.close()
    _pools.clear()
