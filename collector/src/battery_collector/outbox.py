"""로컬 저장 후 전송 (store-and-forward).

- 메일박스에서 읽은 이벤트는 **먼저 여기 커밋**하고, 그 다음에 PLC에 ACK를 쓴다.
  수집기가 죽어도 ACK 안 한 레코드는 PLC에 남아 있고, 커밋한 레코드는 디스크에 남는다.
- 같은 event_id는 한 번만 저장 (PLC가 같은 레코드를 다시 보여줘도 안전).
- 보낸 것은 sent_at을 채우고 keep_days 뒤에 지운다. API가 거절한 것은 dead로 따로 둔다.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
    event_id   TEXT PRIMARY KEY,
    run_id     INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at    TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    dead       INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS event_unsent ON event (run_id, seq) WHERE sent_at IS NULL AND dead = 0;
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Outbox:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")  # ACK 전에 디스크까지
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # ── 수집 쪽 ──
    def _tx(self, fn) -> None:
        """한 트랜잭션 = 한 번의 fsync. 500건을 표시할 때 500번 커밋하지 않게."""
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            fn(c)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def add(self, events: list[dict], state: dict[str, str] | None = None,
            dead: list[tuple[dict, str]] | None = None) -> int:
        """이벤트·dead 이벤트·수집 상태를 한 트랜잭션으로. 새로 들어간 (dead 아닌) 수를 돌려준다."""
        now = _now()
        added = 0

        def run(c):
            nonlocal added
            before = c.total_changes
            c.executemany(
                "INSERT OR IGNORE INTO event (event_id, run_id, seq, body, created_at) VALUES (?, ?, ?, ?, ?)",
                [(e["event_id"], e["run_id"], e["seq"], json.dumps(e, ensure_ascii=False), now) for e in events])
            added = c.total_changes - before
            c.executemany(
                """INSERT OR IGNORE INTO event (event_id, run_id, seq, body, created_at, dead, last_error)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                [(e["event_id"], e["run_id"], e["seq"], json.dumps(e, ensure_ascii=False), now, why[:500])
                 for e, why in (dead or [])])
            for k, v in (state or {}).items():
                c.execute("INSERT INTO state (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v",
                          (k, v))

        self._tx(run)
        return added

    def get_state(self, k: str, default: str | None = None) -> str | None:
        r = self._conn().execute("SELECT v FROM state WHERE k = ?", (k,)).fetchone()
        return r[0] if r else default

    def set_state(self, k: str, v: str) -> None:
        self.add([], {k: v})

    # ── 전송 쪽 ──
    def next_batch(self, limit: int) -> list[dict]:
        rows = self._conn().execute(
            "SELECT body FROM event WHERE sent_at IS NULL AND dead = 0 ORDER BY run_id, seq LIMIT ?",
            (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def mark_sent(self, event_ids: list[str]) -> None:
        now = _now()
        self._tx(lambda c: c.executemany(
            "UPDATE event SET sent_at = ?, attempts = attempts + 1, last_error = NULL WHERE event_id = ?",
            [(now, i) for i in event_ids]))

    def mark_failed(self, event_ids: list[str], error: str) -> None:
        self._tx(lambda c: c.executemany(
            "UPDATE event SET attempts = attempts + 1, last_error = ? WHERE event_id = ?",
            [(error[:500], i) for i in event_ids]))

    def mark_dead(self, event_id: str, error: str) -> None:
        self._tx(lambda c: c.execute(
            "UPDATE event SET dead = 1, attempts = attempts + 1, last_error = ? WHERE event_id = ?",
            (error[:500], event_id)))

    # ── 복구 ──
    def dead_events(self) -> list[tuple[dict, str | None]]:
        rows = self._conn().execute(
            "SELECT body, last_error FROM event WHERE dead = 1 ORDER BY run_id, seq").fetchall()
        return [(json.loads(b), err) for b, err in rows]

    def revive(self, events: list[dict]) -> None:
        """dead 이벤트를 (고친 내용으로) 다시 보낼 대기열에 넣는다. event_id는 그대로."""
        self._tx(lambda c: c.executemany(
            "UPDATE event SET body = ?, dead = 0, sent_at = NULL, last_error = NULL WHERE event_id = ? AND dead = 1",
            [(json.dumps(e, ensure_ascii=False), e["event_id"]) for e in events]))

    def unsend_since(self, since_iso: str) -> int:
        """since 이후에 수집했거나 보낸 이벤트를 다시 보낼 대기열로 (DB 복원 뒤). 이미 있는 건 API가 거른다.

        백업에 들어갔는지는 DB에 들어간 시각(sent_at)이 정한다. 인터넷이 끊겨 늦게 보낸 이벤트는
        수집 시각이 백업 전이어도 백업에 없을 수 있어서 둘 다 본다.
        """
        n = 0

        def run(c):
            nonlocal n
            n = c.execute("""UPDATE event SET sent_at = NULL
                             WHERE sent_at IS NOT NULL AND (sent_at >= ? OR created_at >= ?)""",
                          (since_iso, since_iso)).rowcount
        self._tx(run)
        return n

    def oldest_kept(self) -> str | None:
        r = self._conn().execute("SELECT min(created_at) FROM event").fetchone()
        return r[0] if r else None

    def purge(self, keep_days: int) -> int:
        cut = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        return self._conn().execute("DELETE FROM event WHERE sent_at IS NOT NULL AND sent_at < ?", (cut,)).rowcount

    def stats(self) -> dict:
        c = self._conn()
        r = c.execute("""SELECT count(*) FILTER (WHERE sent_at IS NULL AND dead = 0),
                                count(*) FILTER (WHERE sent_at IS NOT NULL),
                                count(*) FILTER (WHERE dead = 1),
                                max(sent_at)
                         FROM event""").fetchone()
        err = c.execute("""SELECT last_error FROM event WHERE last_error IS NOT NULL
                           ORDER BY rowid DESC LIMIT 1""").fetchone()
        return {"pending": r[0], "sent": r[1], "dead": r[2], "last_sent_at": r[3],
                "last_error": err[0] if err else None,
                "state": dict(c.execute("SELECT k, v FROM state").fetchall())}
