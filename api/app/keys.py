"""API 키 관리 (VM에서 root로 실행). 키 원문은 만들 때 한 번만 화면에 나온다.

관리자:
    sudo battery-api-key add collector-2a --scope read --scope write:2a
    sudo battery-api-key grant 01xooyeon write:2a      # 팀원 키에 권한 추가 (키는 그대로)
    sudo battery-api-key list
    sudo battery-api-key revoke 01xooyeon

팀원 본인 (GitHub 키로 SSH, tunnel-session → battery-api-self → 여기 self):
    ssh tunnel@<서버> new      새 키 발급 (있으면 교체, 권한은 유지)
    ssh tunnel@<서버> info     내 키 정보
    ssh tunnel@<서버> revoke   내 키 폐기
키 이름 = GitHub 아이디. API 접근 로그와 쓴 데이터(api_client)가 이 이름으로 남는다.
"""
import argparse
import datetime as dt
import json
import os
import re
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


GH_USER_LISTS = ("/etc/gh-tunnel-users", "/etc/gh-key-users")
GH_USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def _active(data: dict, name: str) -> list[dict]:
    return [k for k in data["keys"] if k["name"] == name and not k.get("revoked_at")]


def _issue(data: dict, name: str, scopes: list[str], source: str, note: str = "") -> str:
    key = "bdb_" + secrets.token_urlsafe(32)
    data["keys"].append({"name": name, "scopes": sorted(set(scopes)), "sha256": hash_key(key),
                         "prefix": key[:8], "created_at": _now(), "source": source, "note": note})
    return key


def _revoke(keys: list[dict], by: str) -> None:
    for k in keys:
        k["revoked_at"] = _now()
        k["revoked_by"] = by


def _check_scopes(scopes) -> bool:
    bad = set(scopes) - SCOPES
    if bad:
        print(f"unknown scope: {', '.join(sorted(bad))} (allowed: {', '.join(sorted(SCOPES))})", file=sys.stderr)
    return not bad


def cmd_add(a) -> int:
    data = _load()
    if _active(data, a.name):
        print(f"'{a.name}' already has an active key. revoke it first.", file=sys.stderr)
        return 1
    scopes = a.scope or ["read"]
    if not _check_scopes(scopes):
        return 1
    key = _issue(data, a.name, scopes, "admin", a.note or "")
    _save(data)
    print(f"name:   {a.name}\nscopes: {' '.join(sorted(set(scopes)))}\nkey:    {key}\n")
    print("이 키는 다시 볼 수 없습니다. 지금 복사해서 본인에게만 전달하세요.")
    return 0


def cmd_grant(a) -> int:
    """키를 바꾸지 않고 권한만 더한다 (팀원 self 키에 write:2a 주기 등)."""
    data = _load()
    hit = _active(data, a.name)
    if not hit:
        print(f"no active key named '{a.name}' (본인이 먼저 'ssh tunnel@<서버> new'로 발급)", file=sys.stderr)
        return 1
    if not _check_scopes(a.scopes):
        return 1
    for k in hit:
        k["scopes"] = sorted(set(k["scopes"]) | set(a.scopes))
    _save(data)
    print(f"{a.name}: {' '.join(hit[0]['scopes'])} (다음 요청부터 반영)")
    return 0


def _gh_users() -> set[str]:
    users = set()
    for f in GH_USER_LISTS:
        try:
            with open(f, encoding="utf-8") as fh:
                users |= {u.split("#")[0].strip() for u in fh if u.split("#")[0].strip()}
        except FileNotFoundError:
            pass
    return users


SELF_HELP = """battery-db API 키 (본인용)

  ssh tunnel@<서버> new      새 키 발급. 이미 있으면 이전 키는 바로 막히고 새 키로 교체 (권한은 유지)
  ssh tunnel@<서버> info     내 키 정보 (키 원문은 다시 볼 수 없음)
  ssh tunnel@<서버> revoke   내 키 폐기

사용: 요청 헤더 X-API-Key: <키>  (또는 Authorization: Bearer <키>)
문서: https://<API 도메인>/docs   내 사용량: GET /me
쓰기 권한(write:2a / write:pack)은 관리자에게 요청."""


def cmd_self(a) -> int:
    """tunnel 계정 SSH 세션에서만 불린다. user는 접속에 쓴 GitHub 키로 정해진다."""
    user, action = a.user, (a.action or "info").strip()
    if not GH_USER_RE.match(user) or user not in _gh_users():
        print("등록된 GitHub 계정이 아닙니다.", file=sys.stderr)
        return 1
    data = _load()
    hit = _active(data, user)
    if action == "new":
        scopes = hit[0]["scopes"] if hit else ["read"]
        _revoke(hit, "self-rotate")
        key = _issue(data, user, scopes, "self")
        _save(data)
        print(f"name:   {user}\nscopes: {' '.join(scopes)}\nkey:    {key}\n")
        print("이 키는 다시 볼 수 없습니다. 비밀번호처럼 보관하고 저장소·채팅에 올리지 마세요."
              + ("\n이전 키는 이제 막혔습니다." if hit else ""))
    elif action == "info":
        if hit:
            k = hit[0]
            print(f"name:    {user}\nscopes:  {' '.join(k['scopes'])}\nkey:     {k['prefix']}…\ncreated: {k['created_at']}\n")
        else:
            print(f"{user}: 발급된 키 없음\n")
        print(SELF_HELP)
    elif action == "revoke":
        if not hit:
            print("폐기할 키가 없습니다.")
            return 0
        _revoke(hit, "self")
        _save(data)
        print("키를 폐기했습니다 (다음 요청부터 막힘).")
    else:
        print(SELF_HELP)
        return 0 if action in ("help", "") else 1
    return 0


def cmd_prune_gh(a) -> int:
    """GitHub 허용 목록에서 빠진 팀원의 self 키를 폐기 (sync-gh-keys.sh가 5분마다 호출)."""
    data = _load()
    users = _gh_users()
    gone = [k for k in data["keys"] if k.get("source") == "self" and not k.get("revoked_at")
            and k["name"] not in users]
    if gone:
        _revoke(gone, "removed-from-gh-list")
        _save(data)
        print("revoked: " + ", ".join(k["name"] for k in gone))
    return 0


def cmd_list(a) -> int:
    for k in _load()["keys"]:
        if k.get("revoked_at") and not a.all:
            continue
        state = f"revoked {k['revoked_at']} ({k.get('revoked_by', '')})" if k.get("revoked_at") else "active"
        print(f"{k['name']:<20} {k['prefix']}…  {' '.join(k['scopes']):<24} {k.get('source', 'admin'):<5} "
              f"{k['created_at']}  {state}  {k.get('note', '')}")
    return 0


def cmd_revoke(a) -> int:
    data = _load()
    hit = _active(data, a.name)
    if not hit:
        print(f"no active key named '{a.name}'", file=sys.stderr)
        return 1
    _revoke(hit, "admin")
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
    s = sub.add_parser("list", help="키 목록 (기본: 사용 중인 것만)")
    s.add_argument("--all", action="store_true", help="폐기된 키도")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("revoke", help="키 폐기")
    s.add_argument("name")
    s.set_defaults(fn=cmd_revoke)
    s = sub.add_parser("grant", help="키는 그대로 두고 scope 추가")
    s.add_argument("name")
    s.add_argument("scopes", nargs="+")
    s.set_defaults(fn=cmd_grant)
    s = sub.add_parser("self", help="(SSH 셀프서비스 전용) 본인 키 new/info/revoke")
    s.add_argument("user")
    s.add_argument("action", nargs="?", default="info")
    s.set_defaults(fn=cmd_self)
    sub.add_parser("prune-gh", help="GitHub 목록에서 빠진 팀원의 self 키 폐기").set_defaults(fn=cmd_prune_gh)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
