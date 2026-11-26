import json

import httpx
import pytest

from battery_collector.config import Api
from battery_collector.outbox import Outbox
from battery_collector.uploader import AuthError, Uploader


def ev(seq, station="2A-IN"):
    return {"event_id": f"00000000-0000-0000-0000-{seq:012d}", "run_id": 1, "seq": seq, "source": "2A",
            "station_code": station, "event_type": "cell.created", "entity_id": f"R0001-{seq:06d}",
            "payload": {}, "occurred_at": "2026-11-26T00:00:00+00:00"}


def make(tmp_path, handler, batch_max=500):
    ob = Outbox(tmp_path / "ob.sqlite3")
    api = Api(base_url="http://test", key="bdb_test", batch_max=batch_max)
    client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    return ob, Uploader(api, ob, client)


def test_sends_in_batches_and_marks_sent(tmp_path):
    calls = []

    def h(req):
        body = json.loads(req.content)
        calls.append(len(body["events"]))
        assert req.headers["X-API-Key"] == "bdb_test"
        return httpx.Response(200, json={"received": len(body["events"]), "inserted": len(body["events"]),
                                         "duplicates": 0})

    ob, up = make(tmp_path, h, batch_max=3)
    ob.add([ev(i) for i in range(1, 8)])
    while up.flush_once():
        pass
    assert calls == [3, 3, 1]
    assert ob.stats()["pending"] == 0 and ob.stats()["sent"] == 7


def test_422_isolates_bad_event(tmp_path):
    def h(req):
        evs = json.loads(req.content)["events"]
        if any(e["station_code"] == "2A-NOPE" for e in evs):
            return httpx.Response(422, json={"detail": {"unknown_stations": ["2A-NOPE"]}})
        return httpx.Response(200, json={"inserted": len(evs), "duplicates": 0})

    ob, up = make(tmp_path, h)
    ob.add([ev(1), ev(2, "2A-NOPE"), ev(3)])
    assert up.flush_once() == 3
    s = ob.stats()
    assert (s["pending"], s["sent"], s["dead"]) == (0, 2, 1)
    assert "unknown_stations" in s["last_error"]


def test_server_error_keeps_events(tmp_path):
    ob, up = make(tmp_path, lambda req: httpx.Response(503, text="down"))
    ob.add([ev(1)])
    with pytest.raises(httpx.HTTPStatusError):
        up.flush_once()
    assert ob.stats()["pending"] == 1


def test_network_error_keeps_events(tmp_path):
    def h(req):
        raise httpx.ConnectError("no route")

    ob, up = make(tmp_path, h)
    ob.add([ev(1)])
    with pytest.raises(httpx.ConnectError):
        up.flush_once()
    assert ob.stats()["pending"] == 1


def test_bad_key_raises_auth_error(tmp_path):
    ob, up = make(tmp_path, lambda req: httpx.Response(401, json={"detail": "bad key"}))
    ob.add([ev(1)])
    with pytest.raises(AuthError):
        up.flush_once()
    assert ob.stats()["pending"] == 1


def test_outbox_ignores_duplicate_event_ids(tmp_path):
    ob = Outbox(tmp_path / "ob.sqlite3")
    assert ob.add([ev(1), ev(2)]) == 2
    assert ob.add([ev(2), ev(3)]) == 1
    assert ob.stats()["pending"] == 3


def test_health_report_posts_and_survives_errors(tmp_path):
    seen = []

    def h(req):
        if req.url.path == "/collector/health":
            seen.append(json.loads(req.content))
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(200, json={"inserted": 0, "duplicates": 0})

    ob = Outbox(tmp_path / "ob.sqlite3")
    api = Api(base_url="http://test", key="bdb_test")
    up = Uploader(api, ob, httpx.Client(base_url="http://test", transport=httpx.MockTransport(h)),
                  health_fn=lambda: {"line": "2A", "lost": 3})
    up.backoff_s = 4.0
    assert up.report_health()
    assert seen == [{"line": "2A", "lost": 3, "upload_backoff_s": 4.0}]

    def boom(req):
        raise httpx.ConnectError("down")

    up2 = Uploader(api, ob, httpx.Client(base_url="http://test", transport=httpx.MockTransport(boom)),
                   health_fn=lambda: {"line": "2A"})
    assert up2.report_health() is False  # 예외를 밖으로 던지지 않음


def test_replay_and_revive(tmp_path):
    ob = Outbox(tmp_path / "ob.sqlite3")
    ob.add([ev(1), ev(2)], dead=[(ev(3, "PLC-ST99"), "unknown station")])
    ob.mark_sent([ev(1)["event_id"], ev(2)["event_id"]])
    assert ob.stats()["pending"] == 0 and ob.stats()["dead"] == 1
    assert ob.unsend_since("2000-01-01T00:00:00+00:00") == 2
    assert ob.stats()["pending"] == 2
    (dead, err), = ob.dead_events()
    assert dead["station_code"] == "PLC-ST99" and err == "unknown station"
    ob.revive([{**dead, "station_code": "2A-IN"}])
    s = ob.stats()
    assert (s["pending"], s["dead"]) == (3, 0)
    assert any(e["station_code"] == "2A-IN" and e["seq"] == 3 for e in ob.next_batch(10))


def test_replay_includes_events_sent_after_since(tmp_path):
    # 05:59 수집 → 인터넷 끊김 → 06:05 전송. 06:00 백업에는 없으므로 replay --since 06:00에 들어가야 함
    ob = Outbox(tmp_path / "ob.sqlite3")
    ob.add([ev(1)])
    c = ob._conn()
    c.execute("UPDATE event SET created_at = '2026-11-26T05:59:00+00:00', sent_at = '2026-11-26T06:05:00+00:00'")
    assert ob.unsend_since("2026-11-26T06:00:00+00:00") == 1
    c.execute("UPDATE event SET created_at = '2026-11-26T05:00:00+00:00', sent_at = '2026-11-26T05:01:00+00:00'")
    assert ob.unsend_since("2026-11-26T06:00:00+00:00") == 0  # 백업 전에 들어간 건 그대로


def test_missing_run_stops_instead_of_dead(tmp_path):
    from battery_collector.uploader import MissingReference

    def h(req):
        return httpx.Response(422, json={"detail": "unknown reference: Key (run_id)=(15) is not present"})

    ob, up = make(tmp_path, h)
    ob.add([ev(1), ev(2)])
    with pytest.raises(MissingReference):
        up.flush_once()
    s = ob.stats()
    assert (s["pending"], s["dead"]) == (2, 0)
