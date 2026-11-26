# battery-collector — PLC 이벤트 수집기

PLC 이벤트 메일박스(링 버퍼)를 읽어 로컬에 먼저 저장하고, battery-db API `POST /events`로 묶어서 보낸다.
라인 PC(Factory I/O·PLC 옆)에서 돈다. 클라우드에서 돌리지 않는다.

```
 PLC (GX Simulator2 / Q PLC)
   │  D2700~D2799 메일박스 (래더가 씀)
   ▼
 PlcPort ── fake | modbus | mx | 브리지 안에서 직접
   │  100 ms 폴링, 한 번에 100워드
   ▼
 Collector ── 레코드 → 이벤트 (event_id = uuid5(run, seq))
   │  1) outbox에 커밋  2) PLC에 ACK (D2701)
   ▼
 Outbox (SQLite, WAL) ── 인터넷이 끊겨도 여기 쌓임
   │  별도 스레드, 1초마다 최대 500건
   ▼
 API POST /events ── event_id로 중복 제거 → event_log
```

## 보장

| 상황 | 결과 |
|---|---|
| 인터넷·API 끊김 | outbox에 쌓이고, 복구되면 밀린 것부터 보냄. 수집은 멈추지 않음 |
| 수집기가 죽음 (커밋 전) | ACK 안 했으니 PLC 레코드가 남아 있어 재시작 후 다시 읽음 |
| 수집기가 죽음 (커밋 후 ACK 전) | 다시 읽지만 같은 event_id라 outbox·DB에 한 번만 |
| 같은 이벤트를 다시 보냄 | API가 `duplicates`로 세고 넣지 않음 |
| 수집기가 너무 느림 | PLC 링(8칸)이 차면 PLC가 넘침 수(D2702)를 올리고 버림. 수집기가 로그로 알림 |
| 순번 16비트 넘김 (65535 → 0) | 수집기가 run 안에서 계속 증가하는 seq로 풀어서 event_id가 겹치지 않음 |
| 설정에 없는 스테이션 번호 | 보내지 않고 outbox에 dead로 원본째 남김 + 로그 (ACK는 함, PLC가 막히지 않게) |
| PLC·래더 재시작 (메일박스가 0으로) | 쓰기 순번이 링 범위 밖으로 튀면 재시작으로 보고 PLC 쪽 순번에 다시 맞춤. seq는 계속 증가 |
| 설정에 없는 이벤트 코드 | `plc.unknown_<코드>`로 원본 그대로 보냄 |
| API가 일부 이벤트를 거절 (422) | 1건씩 다시 보내 문제 있는 것만 dead로 빼고 나머지는 보냄 |
| 키가 틀림·권한 없음 (401/403) | 보내기만 멈추고 계속 쌓음 (키를 고치면 이어서 보냄) |

## 메일박스 (가정)

digital-twin-a `docs/2a-data-design.md` 초안 그대로. **PLC 담당과 합의 전이다.** 합의되면 [`config/2a.toml`](config/2a.toml)만 고친다.

| 디바이스 | 내용 | 쓰는 쪽 |
|---|---|---|
| D2700 | 쓰기 순번 (16비트, 레코드를 다 쓴 **뒤에** +1) | PLC |
| D2701 | 읽은 순번 = ACK | 수집기 |
| D2702 | 넘침 횟수 | PLC |
| D2703 | 예비 | |
| D2704~D2799 | 레코드 8개 × 12워드. 순번 s → `s % 8` 번째 | PLC |

레코드 12워드: 순번, 이벤트 코드, 스테이션 번호, 레인, 셀 번호(2워드, 하위 먼저), LOT, 결과, 사유, 값1~3.
이벤트 코드·스테이션 번호 표와 값1~3의 뜻은 `config/2a.toml`.

래더가 지킬 것:

1. 레코드 12워드를 다 쓰고 나서 D2700을 올린다 (반대로 하면 수집기가 덜 쓴 레코드를 읽음).
2. `D2700 - D2701 == 8`이면 쓰지 말고 D2702 +1, 알람.
3. 셀 번호는 run(Factory I/O 시작) 안에서 유일하게. DB 셀 ID = `R<run 4자리>-<셀 번호 6자리>`.

## 실행

```powershell
cd collector
uv venv .venv; uv pip install --python .venv -e ".[modbus,test]"   # 또는 python -m venv + pip
$env:BATTERY_API_URL = "https://<API 도메인>"
$env:BATTERY_API_KEY = "bdb_..."        # write:2a 권한이 있는 키 (관리자에게)

.venv\Scripts\python -m battery_collector run --config config/2a.toml --new-run --purpose demo
.venv\Scripts\python -m battery_collector run --config config/2a.toml --run-id 12          # 이어서
.venv\Scripts\python -m battery_collector run --config config/2a.toml --run-id 1 --no-upload  # API 없이
.venv\Scripts\python -m battery_collector status --config config/2a.toml
.venv\Scripts\python -m battery_collector end-run --config config/2a.toml --run-id 12
```

- Factory I/O를 다시 시작하면 새 run을 연다 (`--new-run`). 셀 번호가 처음부터 다시 시작하기 때문.
- 키는 환경 변수로만. 설정 파일·저장소에 쓰지 않는다.
- outbox 파일: `collector/data/` (git에 안 올라감).

### Factory I/O·래더를 다시 시작할 때 (순서대로)

PLC 메모리가 초기화되면 아직 안 읽은 메일박스 레코드가 사라진다. 그래서:

1. 라인 정지 (새 이벤트가 안 생기게)
2. `status`에서 `pending`이 0이 될 때까지 기다림 (수집기는 계속 켜 둠)
3. 수집기 종료 (Ctrl+C) → `end-run --run-id <지금 run>`
4. Factory I/O·래더 재시작
5. `run --new-run`

## 누락 확인·복구

수집기는 30초마다 상태를 `POST /collector/health`로 보낸다 (멈출 때 한 번 더). DB에서 이렇게 본다:

| 보고 싶은 것 | 어디 | API |
|---|---|---|
| 수집기가 살아 있나, 지금까지 잃은 수 | `v_collector_status` (`stale` = 90초 넘게 보고 없음, `dropped_total` = 잃은 레코드 + 건너뛴 레코드 + PLC 넘침) | `GET /collectors` |
| DB에 빠진 이벤트 순번 | `v_event_gaps` | `GET /runs/{run_id}/gaps` |
| 단계가 빠진 셀 (래더가 이벤트를 안 올린 경우 포함) | `v_cell_incomplete` (run이 끝났거나 10분 넘게 소식 없는 셀만) | 같은 응답의 `incomplete_cells` |

PLC가 넘침으로 버린 이벤트는 순번을 받지 못해 `v_event_gaps`에 안 나온다. `plc_overflow`로 본다.

복구:

```powershell
# 설정에 스테이션 번호 등을 추가한 뒤, dead로 남은 이벤트를 다시 만들어 보냄
.venv\Scripts\python -m battery_collector resend-dead --config config/2a.toml --dry-run
.venv\Scripts\python -m battery_collector resend-dead --config config/2a.toml

# DB를 백업에서 복원한 뒤: 백업 시각 이후 수집했거나 보낸 이벤트를 다시 보냄 (이미 있는 건 API가 거름)
.venv\Scripts\python -m battery_collector replay --config config/2a.toml --since 2026-11-26T09:00+09:00
```

outbox는 보낸 이벤트를 `keep_days`(14일) 동안 갖고 있다. 백업이 6시간마다라도, 그 사이 이벤트는 `replay`로 다시 채울 수 있다.

**복원 뒤 replay 순서 (관리자):**

1. 수집기를 멈춘다. 복원한다.
2. 백업 뒤에 만든 run은 복원된 DB에 없다. replay 전에 **같은 run_id로** 다시 넣고 번호 발급기를 맞춘다.
   안 그러면 다음 `POST /runs`가 같은 번호를 다른 run에 줘서 이벤트가 섞인다.
   ```sql
   -- 수집기 PC의 status에 나온 run_id (예: 15)
   INSERT INTO sim_run (run_id, line, purpose, note) OVERRIDING SYSTEM VALUE
   VALUES (15, '2A', 'demo', 'restored: recreated after DB restore');
   SELECT setval(pg_get_serial_sequence('sim_run', 'run_id'), (SELECT max(run_id) FROM sim_run));
   ```
3. `replay --since <백업 시각>`.

run이 없으면 수집기는 이벤트를 dead로 만들지 않고 전송을 멈춘 채 1분마다 다시 시도한다 (로그 "not in DB (restored from backup?)").

**한계:** `v_event_gaps`는 사이에 빈 순번만 잡는다. run의 끝부분이 통째로 빠진 경우(복원 손실이 보통 이 모양)는
`GET /runs/{run_id}/gaps`의 `tail_missing`(수집기가 보고한 마지막 순번 − DB의 마지막 순번)으로 본다.

## PLC 연결 (`[plc].backend`)

| backend | 언제 | 상태 |
|---|---|---|
| `fake` | PLC 없이 개발·시험. 가짜 셀이 투입 → 검사 → 분류 → 박스까지 흐름 | 시험함 |
| `modbus` | 실제 Q PLC(MODBUS/TCP 모듈) 또는 Modbus로 열린 PLC. `d_to_hr`로 D ↔ HR 매핑 | pymodbus 서버로 시험함 |
| `mx` | MX Component로 GX Simulator2 직접 | **시험 안 함** (이 PC에 MX Component 없음) |
| 브리지 안에서 | 브리지가 이미 PLC에 붙어 있을 때 (권장) | 아래 |

digital-twin-a `docs/interface.md` 규칙상 **PLC에 쓰는 프로그램은 브리지 하나**다. 수집기는 ACK(D2701)를 쓰므로,
브리지가 돌고 있으면 수집기를 브리지 프로세스 안에서 돌리고 브리지의 PLC 연결을 넘긴다:

```python
from battery_collector import config
from battery_collector.collector import Collector
from battery_collector.outbox import Outbox
from battery_collector.uploader import Uploader

class BridgePort:                       # 브리지가 가진 MX 연결로 구현
    def read_words(self, d_start, count): return bridge.read_d(d_start, count)
    def write_word(self, d_addr, value):  bridge.write_d(d_addr, value)
    def close(self): pass

cfg = config.load("config/2a.toml")
col = Collector(cfg.mailbox, cfg.line, BridgePort(), Outbox(cfg.outbox_path), run_id)
up = Uploader(cfg.api, Outbox(cfg.outbox_path)); up.start()
# 브리지 루프 안에서 100 ms마다:
col.poll_once()
```

## 시험

```powershell
.venv\Scripts\python -m pytest -q
```

- 단위: 링 순환·넘침·덜 쓴 레코드·16비트 순번, 32비트 셀 번호, event_id 규칙(API와 같은지), 엔티티 ID
- 수집 루프 (가짜 PLC): 순서·빠짐·중복 없음, ACK 유실 후 재시작, 수집기 정지 → PLC 넘침, 모르는 스테이션·코드, PLC 재시작
- 업로드 (가짜 HTTP): 500건씩 묶음, 422 격리, 5xx·네트워크 오류 보존, 401
- Modbus: 실제 Modbus TCP 서버에 레코드를 쓰고 수집 + ACK

**11/26 실제 API 시험** (`battery_test` DB에 API를 :8001로 띄움, 끝나고 지움):

| 시험 | 결과 |
|---|---|
| 가짜 PLC 120셀/분, 90초 | 이벤트 1,178건 → DB 1,178행, seq 1~1178 빠짐 없음, 셀 179개, 박스 20개 |
| 같은 1,178건 전부 다시 보냄 | `inserted 0, duplicates 1178` |
| 수집 중 20초 동안 인터넷 끊김 | 끊긴 동안 outbox에 쌓임(중간 확인 때 203건), 복구 후 전부 전송. 464건 → DB 464행, 잃은 것 0 |

**11/26 누락 확인·복구 시험** (`battery_test`, 스키마 01~06, 이 브랜치 API를 :8001로, 끝나고 지움):

| 시험 | 결과 |
|---|---|
| 45초 수집 + 상태 보고 | `GET /collectors`: stale false, events 464, dropped_total 0, outbox_pending 0 |
| DB에서 seq 50~52 삭제 (중간 손실) | `GET /runs/2/gaps`: missing 3, 구간 (50, 52). 해당 셀이 incomplete에 `created`·`id_read` 빠짐으로 나옴 |
| `replay --since <수집 시작>` | 464건 다시 보냄 → inserted 3, duplicates 461 → missing 0 |
| API 스모크 (`api/tests/api_smoke.py`) | 41개 전부 통과 |

`v_cell_incomplete`에 63개가 `packed_or_scrapped` 빠짐으로 남은 것은 정상이다: 45초에 수집을 멈춰서 라인 위에 있던 셀들 (가짜 공정은 셀 하나에 약 35초).

## 아직 아닌 것

- 메일박스 배치·이벤트 코드·스테이션 번호는 **가정**이다 (PLC 담당과 합의 필요).
- `mx` 백엔드는 실제 MX Component에서 안 돌려 봄.
- `occurred_at`은 PLC 시각이 아니라 수집기가 읽은 시각 (최대 폴링 주기 100 ms 늦음). 정확한 시각이 필요하면 레코드에 PLC 시각을 넣어야 한다.
- 링 8칸 = 폴링 한 번(100 ms) 사이에 8건 넘게 몰리면 넘친다. 실제 래더의 몰림을 보고 칸 수를 정한다.
- 이벤트 → `cell`·`cell_inspection` 등 테이블 반영은 아직 없다 (`event_log`에만 들어감).
