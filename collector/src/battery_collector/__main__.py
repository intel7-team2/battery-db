"""수집기 실행.

    python -m battery_collector run --config config/2a.toml --new-run --purpose demo
    python -m battery_collector run --config config/2a.toml --run-id 12
    python -m battery_collector run --config config/2a.toml --run-id 1 --no-upload   # API 없이 로컬만
    python -m battery_collector status --config config/2a.toml
    python -m battery_collector end-run --config config/2a.toml --run-id 12

환경 변수: BATTERY_API_KEY (필수, 업로드할 때), BATTERY_API_URL (설정 파일보다 우선)
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
import sys
import threading
import time

import httpx

from . import config as cfgmod
from .collector import Collector
from .outbox import Outbox
from .plc import PlcError, open_port
from .uploader import Uploader

log = logging.getLogger("collector")


def _http(cfg: cfgmod.Config) -> httpx.Client:
    if not cfg.api.key:
        sys.exit("BATTERY_API_KEY is not set")
    return httpx.Client(base_url=cfg.api.base_url, timeout=cfg.api.timeout_s,
                        headers={"X-API-Key": cfg.api.key, "User-Agent": "battery-collector"})


def cmd_run(cfg: cfgmod.Config, a: argparse.Namespace) -> int:
    outbox = Outbox(cfg.outbox_path)
    run_id = a.run_id
    if a.new_run:
        with _http(cfg) as h:
            r = h.post("/runs", json={"line": cfg.line, "purpose": a.purpose, "note": a.note,
                                      "scene_version": a.scene_version, "ladder_version": a.ladder_version})
            r.raise_for_status()
            run_id = r.json()["run_id"]
        log.info("new run %d (purpose=%s)", run_id, a.purpose)
    if run_id is None:
        sys.exit("give --run-id N or --new-run")

    port = open_port(cfg)
    col = Collector(cfg.mailbox, cfg.line, port, outbox, run_id)
    up = None if a.no_upload else Uploader(cfg.api, Outbox(cfg.outbox_path), _http(cfg))
    if up:
        up.start()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())

    period = cfg.poll_ms / 1000
    plc_delay = 0.0
    next_report = next_purge = time.monotonic()
    end_at = time.monotonic() + a.duration if a.duration else None
    log.info("polling %s every %d ms (D%d x%d), outbox %s", cfg.plc_backend, cfg.poll_ms,
             cfg.mailbox.base, cfg.mailbox.size, cfg.outbox_path)
    try:
        while not stop.is_set() and (end_at is None or time.monotonic() < end_at):
            t0 = time.monotonic()
            try:
                col.poll_once()
                plc_delay = 0.0
            except PlcError as e:
                plc_delay = min(10.0, max(0.5, plc_delay * 2))
                log.warning("PLC: %s (retry in %.1fs)", e, plc_delay)
            except sqlite3.OperationalError as e:  # outbox 잠김·디스크 문제. ACK 전이라 다음 폴링에 다시 읽음
                plc_delay = min(10.0, max(0.5, plc_delay * 2))
                log.error("outbox: %s (retry in %.1fs)", e, plc_delay)
            if t0 >= next_report:
                next_report = t0 + 30
                s = outbox.stats()
                log.info("run %d seq %s | events %d, pending %d, sent %d, dead %d, lost %d, PLC overflow %d",
                         run_id, col.last_total, col.c.events, s["pending"], s["sent"], s["dead"], col.c.lost,
                         col.c.plc_overflow)
            if t0 >= next_purge:
                next_purge = t0 + 3600
                outbox.purge(cfg.keep_days)
            stop.wait(plc_delay or max(0.0, period - (time.monotonic() - t0)))
    finally:
        log.info("stopping")
        port.close()
        if up:
            up.stop()
    print(json.dumps({"run_id": run_id, **col.c.__dict__, "outbox": outbox.stats()}, ensure_ascii=False, indent=2))
    return 0


def cmd_status(cfg: cfgmod.Config, a: argparse.Namespace) -> int:
    print(json.dumps(Outbox(cfg.outbox_path).stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_end_run(cfg: cfgmod.Config, a: argparse.Namespace) -> int:
    with _http(cfg) as h:
        r = h.post(f"/runs/{a.run_id}/end", json={"note": a.note} if a.note else None)
        print(r.status_code, r.text)
        return 0 if r.status_code == 200 else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="battery_collector")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--run-id", type=int)
    r.add_argument("--new-run", action="store_true", help="POST /runs로 새 run을 연다")
    r.add_argument("--purpose", default="demo")
    r.add_argument("--note")
    r.add_argument("--scene-version")
    r.add_argument("--ladder-version")
    r.add_argument("--no-upload", action="store_true", help="API로 보내지 않고 outbox에만 쌓는다")
    r.add_argument("--duration", type=float, help="초. 시험용")
    sub.add_parser("status")
    e = sub.add_parser("end-run")
    e.add_argument("--run-id", type=int, required=True)
    e.add_argument("--note")
    for s in sub.choices.values():
        s.add_argument("--config", required=True)
        s.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = cfgmod.load(a.config)
    return {"run": cmd_run, "status": cmd_status, "end-run": cmd_end_run}[a.cmd](cfg, a)


if __name__ == "__main__":
    sys.exit(main())
