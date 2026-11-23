"""API 키 관리 (VM에서 root로 실행). 키 원문은 만들 때 한 번만 화면에 나온다.

    sudo battery-api-key add 01xooyeon --scope read
    sudo battery-api-key add collector-2a --scope read --scope write:2a
    sudo battery-api-key list
    sudo battery-api-key revoke 01xooyeon
"""
import argparse
import datetime as dt
import json
import os
import secrets
import shutil
import sys
import tempfile

from . import config
from .auth import SCOPES, hash_key


def _load() -> dict:
    if config.KEYS_FILE.exists():
        return json.loads(config.KEYS_FILE.read_text(encoding="utf-8"))
    return {"keys": []}


def _save(data: dict) -> None:
    path = config.KEYS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".keys-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o640)
    try:
        shutil.chown(tmp, group="battery-api")  # 서비스 계정이 읽을 수 있게
    except (LookupError, PermissionError):
        pass
    os.replace(tmp, path)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def cmd_add(a) -> int:
    data = _load()
    if any(k["name"] == a.name and not k.get("revoked_at") for k in data["keys"]):
        print(f"'{a.name}' already has an active key. revoke it first.", file=sys.stderr)
        return 1
    scopes = sorted(set(a.scope or ["read"]))
    bad = set(scopes) - SCOPES
    if bad:
        print(f"unknown scope: {', '.join(sorted(bad))} (allowed: {', '.join(sorted(SCOPES))})", file=sys.stderr)
        return 1
    key = "bdb_" + secrets.token_urlsafe(32)
    data["keys"].append({"name": a.name, "scopes": scopes, "sha256": hash_key(key),
                         "prefix": key[:8], "created_at": _now(), "note": a.note or ""})
    _save(data)
    print(f"name:   {a.name}\nscopes: {' '.join(scopes)}\nkey:    {key}\n")
    print("이 키는 다시 볼 수 없습니다. 지금 복사해서 본인에게만 전달하세요.")
    return 0


def cmd_list(a) -> int:
    for k in _load()["keys"]:
        state = f"revoked {k['revoked_at']}" if k.get("revoked_at") else "active"
        print(f"{k['name']:<20} {k['prefix']}…  {' '.join(k['scopes']):<28} {k['created_at']}  {state}  {k.get('note', '')}")
    return 0


def cmd_revoke(a) -> int:
    data = _load()
    hit = [k for k in data["keys"] if k["name"] == a.name and not k.get("revoked_at")]
    if not hit:
        print(f"no active key named '{a.name}'", file=sys.stderr)
        return 1
    for k in hit:
        k["revoked_at"] = _now()
    _save(data)
    print(f"revoked '{a.name}' (다음 요청부터 막힘)")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="battery-api-key")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add", help="새 키 발급")
    s.add_argument("name")
    s.add_argument("--scope", action="append", help=f"{', '.join(sorted(SCOPES))} (여러 번 가능, 기본 read)")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_add)
    sub.add_parser("list", help="키 목록").set_defaults(fn=cmd_list)
    s = sub.add_parser("revoke", help="키 폐기")
    s.add_argument("name")
    s.set_defaults(fn=cmd_revoke)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
