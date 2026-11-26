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
