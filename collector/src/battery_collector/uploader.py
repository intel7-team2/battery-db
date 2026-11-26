"""로컬 outbox → API POST /events. 수집(폴링)과 다른 스레드에서 돈다.

- 묶어서 보낸다 (최대 500건). 오사카 왕복이 수십 ms라 1건씩 보내면 초당 20건 남짓에서 막힌다.
- 네트워크 오류·5xx·429: 지수 백오프로 다시 시도. 이벤트는 outbox에 그대로 있다.
- 401/403: 설정 문제(키·권한). 보내기를 멈추고 계속 로그를 남긴다 (수집은 계속, outbox에 쌓임).
- 422: 묶음 전체가 거절된다 (API는 전부 또는 전무). 1건씩 다시 보내서 문제 있는 이벤트만 dead로 뺀다.
"""
from __future__ import annotations

import logging
import random
import threading
import time

import httpx

from .config import Api
from .outbox import Outbox

log = logging.getLogger("collector.upload")


class AuthError(RuntimeError):
    pass


class Uploader:
    def __init__(self, api: Api, outbox: Outbox, client: httpx.Client | None = None):
        if not api.base_url or "<" in api.base_url:
            raise ValueError("API URL not set: BATTERY_API_URL or [api].base_url")
        if not api.key:
            raise ValueError("API key not set: environment variable BATTERY_API_KEY")
        self.api = api
        self.outbox = outbox
        self.http = client or httpx.Client(base_url=api.base_url, timeout=api.timeout_s)
        self.http.headers.update({"X-API-Key": api.key, "User-Agent": "battery-collector"})
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.backoff_s = 0.0

    # ── 한 번 보내기 (시험에서 직접 부름) ──
    def _post(self, events: list[dict]) -> httpx.Response:
        return self.http.post("/events", json={"events": events})

    def flush_once(self) -> int:
        """보낼 것이 있으면 한 묶음 보낸다. 보낸(또는 dead로 뺀) 수를 돌려준다. 실패하면 예외."""
        batch = self.outbox.next_batch(self.api.batch_max)
        if not batch:
            return 0
        ids = [e["event_id"] for e in batch]
        try:
            r = self._post(batch)
        except httpx.HTTPError as e:
            self.outbox.mark_failed(ids, f"network: {e}")
            raise
        if r.status_code == 200:
            body = r.json()
            self.outbox.mark_sent(ids)
            log.info("sent %d (inserted %s, duplicates %s)", len(ids), body.get("inserted"), body.get("duplicates"))
            return len(ids)
        if r.status_code in (401, 403):
            self.outbox.mark_failed(ids, f"{r.status_code}: {r.text[:200]}")
            raise AuthError(f"API refused key ({r.status_code}): {r.text[:200]}")
        if r.status_code == 422:
            return self._isolate(batch, r.text)
        self.outbox.mark_failed(ids, f"{r.status_code}: {r.text[:200]}")
        r.raise_for_status()
        raise httpx.HTTPStatusError(f"unexpected {r.status_code}", request=r.request, response=r)

    def _isolate(self, batch: list[dict], why: str) -> int:
        log.warning("batch of %d rejected (422), retrying one by one: %s", len(batch), why[:200])
        done = 0
        for e in batch:
            r = self._post([e])
            if r.status_code == 200:
                self.outbox.mark_sent([e["event_id"]])
            elif r.status_code == 422:
                self.outbox.mark_dead(e["event_id"], f"422: {r.text[:400]}")
                log.error("dead event seq=%s type=%s: %s", e["seq"], e["event_type"], r.text[:300])
            elif r.status_code in (401, 403):
                self.outbox.mark_failed([e["event_id"]], f"{r.status_code}: {r.text[:200]}")
                raise AuthError(f"API refused key ({r.status_code}): {r.text[:200]}")
            else:
                self.outbox.mark_failed([e["event_id"]], f"{r.status_code}: {r.text[:200]}")
                r.raise_for_status()
            done += 1
        return done

    # ── 백그라운드 ──
    def _loop(self) -> None:
        delay = 0.0
        while not self._stop.is_set():
            try:
                n = self.flush_once()
                delay = 0.0
                if n == self.api.batch_max:
                    continue  # 밀린 게 더 있음
            except AuthError as e:
                log.error("%s", e)
                delay = 60.0
            except Exception as e:  # 네트워크, 5xx 등
                delay = min(60.0, max(1.0, delay * 2)) * random.uniform(0.8, 1.2)
                log.warning("upload failed, retry in %.1fs: %s", delay, e)
            self.backoff_s = delay
            self._stop.wait(delay or self.api.flush_ms / 1000)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="uploader", daemon=True)
        self._thread.start()

    def stop(self, drain_s: float = 5.0) -> None:
        """멈추기 전에 남은 것을 drain_s 동안 보내 본다. 못 보낸 것은 outbox에 남아 다음 실행에 보낸다."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.api.timeout_s + 1)
        end = time.monotonic() + drain_s
        while time.monotonic() < end:
            try:
                if self.flush_once() == 0:
                    break
            except Exception as e:
                log.warning("drain stopped: %s", e)
                break
        self.http.close()
        self.outbox.close()
