"""API 스모크 시험. 표준 라이브러리만 씀.

smoke.sql을 COMMIT으로 바꿔 넣은 **시험용 DB**에 띄운 API를 대상으로 돌린다 (쓰기 시험이 이벤트를 남기므로
운영 DB 'battery'에는 돌리지 말 것).

    BASE=http://127.0.0.1:8001 READ_KEY=bdb_... WRITE_KEY=bdb_... python tests/api_smoke.py
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("BASE", "http://127.0.0.1:8001")
READ, WRITE = os.environ["READ_KEY"], os.environ["WRITE_KEY"]
NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/intel7-team2/battery-db/event")
failed = 0


def call(method, path, key=None, body=None, bearer=False):
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization" if bearer else "X-API-Key"] = f"Bearer {key}" if bearer else key
    req = urllib.request.Request(BASE + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def check(name, cond, detail=""):
    global failed
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    failed += not cond


# 인증
s, b = call("GET", "/health");                     check("health without key", s == 200 and b["ok"], b)
s, b = call("GET", "/stations");                   check("no key -> 401", s == 401, s)
s, b = call("GET", "/stations", "bdb_wrong");      check("bad key -> 401", s == 401, s)
s, b = call("GET", "/stations", READ, bearer=True); check("bearer header works", s == 200, s)

# 조회
s, st = call("GET", "/stations", READ);            check("stations = 23", s == 200 and len(st) == 23, (s, len(st or [])))
s, b = call("GET", "/stations?line=PACK", READ);   check("stations?line=PACK = 6", len(b) == 6, len(b))
s, b = call("GET", "/criteria/current", READ);     check("criteria current", s == 200 and b["ocv_min_mv"] == 3500, b)
s, runs = call("GET", "/runs?line=2A", READ);      check("runs listed", s == 200 and runs, runs)
run_id = runs[-1]["run_id"]                        # smoke.sql run (가장 오래된 것)
s, r = call("GET", f"/runs/{run_id}", READ)
y = r.get("yield") or {}
check("run yield matches smoke.sql", (y.get("cells_in"), y.get("appearance_ng"), y.get("retested"),
      y.get("retest_recovered"), y.get("removed")) == (24, 1, 2, 1, 2), y)
check("run set_spread present", (r.get("set_spread") or {}).get("reduction_pct") is not None, r.get("set_spread"))
s, b = call("GET", f"/runs/{run_id}/testers", READ)
check("4 tester lanes", len([t for t in b if t["station_code"].startswith("2A-T")]) == 4, b)
s, b = call("GET", f"/runs/{run_id}/equipment", READ); check("equipment endpoint", s == 200, s)
s, boxes = call("GET", f"/boxes?run_id={run_id}", READ); check("boxes listed", s == 200 and boxes, s)
full = next(x for x in boxes if x["cells"] == 4)
s, b = call("GET", f"/boxes/{full['box_id']}", READ); check("box has 4 cells", len(b["cells"]) == 4, b)
cell_id = b["cells"][0]["cell_id"]
s, c = call("GET", f"/cells/{cell_id}", READ)
check("cell with inspections", s == 200 and c["inspections"] and c["electrical_latest"], c)
s, b = call("GET", "/cells/NOPE", READ);            check("unknown cell -> 404", s == 404, s)
s, b = call("GET", "/lots", READ);                  check("lots", s == 200 and b, b)
s, b = call("GET", "/alarms/open", READ);           check("alarms open", s == 200, s)
s, b = call("GET", "/packs/PK-SMOKE/trace", READ);  check("pack trace 4 slots", s == 200 and len(b) == 4, (s, b))

# 쓰기 권한
s, b = call("POST", "/runs", READ, {"line": "2A"});  check("read key cannot write -> 403", s == 403, s)
s, b = call("POST", "/runs", WRITE, {"line": "PACK"}); check("2A key cannot start PACK run -> 403", s == 403, s)
s, b = call("GET", "/stations", WRITE);             check("write-only key cannot read -> 403", s == 403, s)
s, run = call("POST", "/runs", WRITE, {"line": "2A", "purpose": "api-test", "scene_version": "abc123"})
check("start run -> 201", s == 201 and run["run_id"], (s, run))
rid = run["run_id"]

ev = lambda seq, **kw: {"run_id": rid, "seq": seq, "source": "2A", "station_code": "2A-VIS1",
                        "event_type": "cell.id_read", "entity_id": f"R{rid:04d}-{seq:06d}",
                        "payload": {"lot": "1", "seq": seq}, "occurred_at": "2026-11-23T14:03:05+09:00", **kw}
s, b = call("POST", "/events", WRITE, {"events": [ev(1), ev(2), ev(3)]})
check("3 events inserted", s == 200 and (b["inserted"], b["duplicates"]) == (3, 0), (s, b))
check("event_id = uuid5(run/seq)", b["event_ids"][0] == str(uuid.uuid5(NS, f"run:{rid}/seq:1")), b)
s, b = call("POST", "/events", WRITE, {"events": [ev(1), ev(2), ev(3)]})
check("resend -> all duplicates", (b["inserted"], b["duplicates"]) == (0, 3), b)
s, b = call("POST", "/events", WRITE, {"events": [ev(4), ev(4)]})
check("duplicate inside batch -> 1 inserted", (b["inserted"], b["duplicates"]) == (1, 0), b)
s, b = call("POST", "/events", WRITE, {"events": [ev(5, station_code="NOPE")]})
check("unknown station -> 422", s == 422, (s, b))
s, b = call("POST", "/events", WRITE, {"events": [ev(5, station_code="PACK-R1")]})
check("PACK station from 2A -> 422", s == 422, (s, b))
s, b = call("POST", "/events", WRITE, {"events": [ev(5, occurred_at="2026-11-23T14:03:05")]})
check("time without timezone -> 422", s == 422, s)
s, b = call("POST", "/events", WRITE, {"events": [ev(5, run_id=None)]})
check("no event_id and no run_id -> 422", s == 422, s)
s, b = call("POST", "/events", WRITE, {"events": [ev(5, run_id=999999, event_id=str(uuid.uuid4()))]})
check("unknown run -> 422", s == 422, (s, b))
s, b = call("POST", "/events", WRITE, {"events": [ev(5, source="PACK", station_code="PACK-R1")]})
check("2A key posting PACK events -> 403", s == 403, s)
s, b = call("POST", "/events", WRITE, {"events": [ev(5)] * 501})
check("batch over 500 -> 422", s == 422, s)
s, b = call("POST", f"/runs/{rid}/end", WRITE, {"note": "api smoke"}); check("end run", s == 200 and b["ended_at"], (s, b))
s, b = call("POST", f"/runs/{rid}/end", WRITE);     check("end again -> 409", s == 409, s)

print(f"\n{'FAILED ' + str(failed) if failed else 'all passed'}")
sys.exit(1 if failed else 0)
