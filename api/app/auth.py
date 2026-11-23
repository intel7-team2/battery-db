"""API 키 인증.

키 파일(/etc/battery-api/keys.json)에는 키 원문이 아니라 SHA-256만 저장한다.
파일이 바뀌면 다음 요청부터 반영된다 (재시작 필요 없음). 키 관리는 app/keys.py.

요청 헤더: `X-API-Key: bdb_...` 또는 `Authorization: Bearer bdb_...`
"""
import hashlib
import json
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from . import config

# scope → 쓸 수 있는 라인
WRITE_SCOPES = {"write:2a": "2A", "write:pack": "PACK"}
SCOPES = {"read", *WRITE_SCOPES}


@dataclass(frozen=True)
class Client:
    name: str
    scopes: frozenset[str]
    prefix: str = ""
    created_at: str = ""
    source: str = "admin"      # admin = 관리자 발급, self = 본인이 SSH로 발급

    @property
    def lines(self) -> set[str]:
        return {line for s, line in WRITE_SCOPES.items() if s in self.scopes}


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


_cache: dict = {"mtime": None, "by_hash": {}}


def _clients() -> dict[str, Client]:
    try:
        mtime = config.KEYS_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        _cache.update(mtime=None, by_hash={})
        return {}
    if mtime != _cache["mtime"]:
        data = json.loads(config.KEYS_FILE.read_text(encoding="utf-8"))
        _cache["by_hash"] = {
            k["sha256"]: Client(k["name"], frozenset(k["scopes"]), k.get("prefix", ""),
                                k.get("created_at", ""), k.get("source", "admin"))
            for k in data.get("keys", [])
            if not k.get("revoked_at")
        }
        _cache["mtime"] = mtime
    return _cache["by_hash"]


_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="API 키 (bdb_...)")
_bearer = HTTPBearer(auto_error=False, description="Authorization: Bearer bdb_...")


def client(
    request: Request,
    key: str | None = Security(_header),
    bearer: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> Client:
    key = key or (bearer.credentials if bearer else None)
    if not key:
        raise HTTPException(401, "API key required", headers={"WWW-Authenticate": "Bearer"})
    c = _clients().get(hash_key(key))
    if c is None:
        raise HTTPException(401, "invalid API key", headers={"WWW-Authenticate": "Bearer"})
    request.state.client_name = c.name  # 접근 로그(audit.py)가 읽음
    return c


def require(scope: str):
    def dep(c: Client = Depends(client)) -> Client:
        if scope not in c.scopes:
            raise HTTPException(403, f"scope '{scope}' required")
        return c
    return dep


def writer(c: Client = Depends(client)) -> Client:
    if not c.lines:
        raise HTTPException(403, "a write scope (write:2a / write:pack) is required")
    return c
