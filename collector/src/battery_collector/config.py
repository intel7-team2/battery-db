"""TOML 설정 → 검증된 dataclass. 메일박스 배치가 바뀌면 TOML만 고친다."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENTITY_KINDS = ("cell", "box", "none")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    offset: int
    words: int  # 1 또는 2 (2 = 32비트, 하위 워드 먼저)


@dataclass(frozen=True)
class EventDef:
    type: str
    entity: str = "cell"
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class Mailbox:
    base: int
    write_seq: int
    read_seq: int
    overflow: int
    record_base: int
    record_words: int
    record_count: int
    ack: bool
    fields: dict[str, Field]
    stations: dict[int, str]
    events: dict[int, EventDef]
    grades: dict[int, str]

    @property
    def size(self) -> int:
        """한 번에 읽을 워드 수 (헤더 + 레코드 전부)."""
        return self.record_base + self.record_words * self.record_count


@dataclass(frozen=True)
class Api:
    base_url: str
    key: str | None
    timeout_s: float = 10
    batch_max: int = 500
    flush_ms: int = 1000


@dataclass(frozen=True)
class Config:
    line: str
    api: Api
    plc_backend: str
    poll_ms: int
    plc_options: dict = field(default_factory=dict)
    outbox_path: Path = Path("data/outbox.sqlite3")
    keep_days: int = 14
    mailbox: Mailbox | None = None


def _int_keys(d: dict, what: str) -> dict[int, object]:
    try:
        return {int(k): v for k, v in d.items()}
    except ValueError as e:
        raise ConfigError(f"{what}: keys must be integers ({e})") from e


def parse_mailbox(m: dict) -> Mailbox:
    fields = {}
    for name, spec in m["fields"].items():
        if not (isinstance(spec, list) and len(spec) == 2 and spec[1] in (1, 2)):
            raise ConfigError(f"mailbox.fields.{name}: expected [offset, 1|2]")
        fields[name] = Field(int(spec[0]), int(spec[1]))
    for need in ("seq", "event", "station"):
        if need not in fields:
            raise ConfigError(f"mailbox.fields.{need} is required")
    rw = int(m["record_words"])
    for name, f in fields.items():
        if f.offset < 0 or f.offset + f.words > rw:
            raise ConfigError(f"mailbox.fields.{name} does not fit in record_words={rw}")

    events = {}
    for code, e in _int_keys(m["events"], "mailbox.events").items():
        ent = e.get("entity", "cell")
        if ent not in ENTITY_KINDS:
            raise ConfigError(f"mailbox.events.{code}.entity must be one of {ENTITY_KINDS}")
        values = tuple(e.get("values", ()))
        if len(values) > 3:
            raise ConfigError(f"mailbox.events.{code}.values: at most 3 (value1~value3)")
        events[code] = EventDef(e["type"], ent, values)

    mb = Mailbox(
        base=int(m["base"]), write_seq=int(m["write_seq"]), read_seq=int(m["read_seq"]),
        overflow=int(m["overflow"]), record_base=int(m["record_base"]), record_words=rw,
        record_count=int(m["record_count"]), ack=bool(m.get("ack", True)), fields=fields,
        stations={k: str(v) for k, v in _int_keys(m["stations"], "mailbox.stations").items()},
        events=events,
        grades={k: str(v) for k, v in _int_keys(m.get("grades", {}), "mailbox.grades").items()},
    )
    header = {mb.write_seq, mb.read_seq, mb.overflow}
    if len(header) != 3 or max(header) >= mb.record_base:
        raise ConfigError("mailbox header offsets must be distinct and below record_base")
    if mb.record_count < 1:
        raise ConfigError("mailbox.record_count must be >= 1")
    return mb


def load(path: str | Path) -> Config:
    path = Path(path)
    with path.open("rb") as f:
        raw = tomllib.load(f)
    api = raw.get("api", {})
    plc = raw.get("plc", {})
    backend = plc.get("backend", "fake")
    out = raw.get("outbox", {})
    outbox_path = Path(out.get("path", "data/outbox.sqlite3"))
    if not outbox_path.is_absolute():
        outbox_path = path.parent.parent / outbox_path  # collector/ 기준
    batch_max = int(api.get("batch_max", 500))
    if not 1 <= batch_max <= 500:
        raise ConfigError("api.batch_max must be 1..500 (API limit)")
    return Config(
        line=raw["line"],
        api=Api(
            base_url=os.environ.get("BATTERY_API_URL", api.get("base_url", "")).rstrip("/"),
            key=os.environ.get("BATTERY_API_KEY"),
            timeout_s=float(api.get("timeout_s", 10)),
            batch_max=batch_max,
            flush_ms=int(api.get("flush_ms", 1000)),
        ),
        plc_backend=backend,
        poll_ms=int(plc.get("poll_ms", 100)),
        plc_options=dict(plc.get(backend, {})),
        outbox_path=outbox_path,
        keep_days=int(out.get("keep_days", 14)),
        mailbox=parse_mailbox(raw["mailbox"]),
    )
