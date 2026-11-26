# 공용 DB · API · SCADA/HMI 구조 제안

> **상태: 제안 (11/24, 미확정).** 작성 se4nchoi (2A DB·API 담당). 팀 합의 전이다.
> 아래 "현재 상태"는 실제로 돌아가는 사실이고, "제안"은 아직 결정이 아니라 **새 작업을 할 때 고려할 방향**이다.
> 이 문서와 다르게 가야 할 이유가 있으면 바꾸지 말고 먼저 담당자와 이야기한다.

## 1. 현재 상태 (사실, 11/24)

| 구성 | 내용 | 자세히 |
|---|---|---|
| DB | PostgreSQL 16 하나, DB `battery`, 스키마 `public` 하나에 2A·팩 테이블이 같이 있음. Oracle 무료 VM (1 GB 메모리) | [server.md](server.md), [`schema/`](../schema/) |
| 스키마 | `01_tables.sql` 테이블 21개, `02_views.sql` 뷰 9개, `03_seed.sql` 스테이션·기준값, `04_roles.sql` 계정, `05_api.sql` API 추적 | [schema.md](schema.md) |
| DB 계정 | `reader`(조회), `grafana`(조회, 10초 제한, 연결 5개), `writer_2a`, `writer_pack`, `battery`(소유자), `api_audit`(API 내부) | [server.md](server.md) |
| 접속 | DB 포트(5432)는 인터넷에 닫힘. SSH 터널로만. 팀원 SSH 계정 `tunnel`은 포트 포워딩과 API 키 발급만 가능 | [server.md](server.md) |
| API | FastAPI, HTTPS 공개. 조회 엔드포인트(뷰 기반), `POST /runs`, `POST /events`(초안). 사람·프로그램마다 API 키 | [`api/README.md`](../api/README.md) |
| API 키 | 팀원이 `ssh tunnel@<서버 IP> new`로 직접 발급. 키 이름 = GitHub 아이디 | [`api/README.md`](../api/README.md) |
| 추적 | 요청마다 `api_access_log`, 키별 요약 `v_api_usage`, API로 쓴 행에 `api_client` | [`api/README.md`](../api/README.md) |
| 시험 데이터 | `sim_run.purpose = 'fake'` 인 run (현재 run 4). 화면 개발용. `schema/test/fake_cleanup.sql`로 지움 | [`schema/test/`](../schema/test/) |
| 실제 데이터 | **아직 없음.** 2A 수집기 골격은 있음 (가정 메일박스, 가짜 PLC로 시험). PLC 메일박스 합의 대기 | [`collector/`](../collector/README.md) |

서버 IP, 도메인, 비밀번호, API 키는 저장소에 없다. 관리자에게 따로 받는다.

## 2. 제안: 데이터 흐름

```
           ┌──────────── 라인마다 ────────────┐
 Factory I/O ─ 브리지 ─ PLC                       │
               │  │                                │
       조작 HMI ┘  └ 수집기 ──(API 키, HTTPS)──▶ API ──▶ DB (battery)
       (PLC 직접,      (메일박스 읽기)              │       ▲
        DB 안 거침)                               │       │ SQL (grafana 계정, 터널)
                                       앱·스크립트 ┘    Grafana (모니터링, 팀 공통)
                                       (API로 조회)
```

| 무엇 | 제안 | 이유 |
|---|---|---|
| **쓰기** | 모든 라인의 수집기·앱은 **API로** 쓴다 (`POST /events` 등) | 검증·중복 제거·권한·추적을 한 곳에서. DB 쓰기 계정 비밀번호를 여러 곳에 뿌리지 않음 |
| **조회 (앱·스크립트·HMI 조회 화면)** | API로 | 키 하나로 끝, 사용 기록 남음 |
| **조회 (Grafana)** | SQL 직결, `grafana` 계정 | Grafana는 SQL 데이터 소스가 훨씬 강함 (시간 범위, 집계). API로 바꾸면 패널마다 엔드포인트가 필요 |
| **조작 (운전·정지·기준값·알람 확인)** | 라인별 HMI → 브리지 → PLC. **DB·API를 거치지 않는다** | DB가 죽어도 라인을 조작할 수 있어야 함. 기준값을 바꾸면 브리지가 PLC에 쓰고 결과를 이벤트로 남김 |
| **DB 직접 쓰기 계정** (`writer_*`) | 수집기가 API로 옮겨 가면 사람에게는 안 줌. 관리·이관 작업만 | |

## 3. 제안: SCADA / HMI

| 화면 | 도구 | 비고 |
|---|---|---|
| 모니터링 대시보드 (수율, NG율, 검사기 편향, 설비 가동률, 알람 이력, LOT·박스·팩 추적) | **Grafana, 전 팀 공통 방식** | 같은 DB·같은 뷰를 보므로 데이터 소스·쿼리·패널을 재사용 |
| 운전 조작 HMI | 라인(팀)별 자유 | PLC 쪽. 공통화 대상 아님 |
| 2B 고유 화면 (예: 3D 트윈) | 팀 자유 | 데이터는 같은 DB/API에서 |

Grafana 공통 규칙 (제안):

- 데이터 소스: PostgreSQL, 계정 `grafana`, DB `battery`, TLS `disable`(터널이 암호화). 이름은 모두 `battery-db`로 맞춘다 (대시보드 JSON을 서로 가져다 쓸 수 있게).
- 대시보드 JSON은 이 저장소 `grafana/<팀>/` 에 커밋한다 (`grafana/2a/`, `grafana/pack/`, `grafana/2b/`). Grafana 폴더도 팀별.
- 공통 변수: `run` (`SELECT run_id FROM sim_run WHERE line = '$line' ORDER BY run_id DESC`), `line`.
- 수치(수율, NG율, 편차 감소율)는 **패널마다 SQL로 다시 계산하지 말고 DB 뷰를 쓴다** (`v_run_yield`, `v_tester_stats`, `v_set_spread_compare` …). 필요한 뷰가 없으면 패널에 SQL을 늘리지 말고 `02_views.sql`에 뷰를 추가한다. 팀마다 숫자가 달라지는 걸 막기 위해.
- 가짜 데이터는 `purpose <> 'fake'`로 거르거나 run 변수로 고른다.
- 시각은 UTC로 저장됨. Grafana 표시 시간대 Asia/Seoul.

Grafana를 어디서 돌릴지 (미정):

| 방식 | 장점 | 단점 |
|---|---|---|
| A. 각자 PC + SSH 터널 (지금 가능) | 설치 쉬움, 서버 부담 없음 | 화면이 사람마다 다를 수 있음 → JSON을 git으로 공유해야 |
| B. 공용 Grafana 하나 (서버) | 모두 같은 화면, URL 하나 | 지금 VM(1 GB)은 DB+API로 빠듯. Grafana 약 200 MB. 더 큰 VM(A1, 6 GB) 확보 후 권장 |

당장은 A, 서버가 커지면 B를 제안.

## 4. 제안: 새 라인·팀이 붙을 때 (예: 2B)

1. 쓸 테이블을 정한다. **한 테이블은 한 팀만 쓴다** (다른 팀 테이블은 읽기만). 새 테이블은 `01_tables.sql`에 섹션을 추가하거나 새 번호 파일(`06_2b.sql` 등)로.
2. `station`에 라인 스테이션을 넣는다 (`line` CHECK에 `'2B'`는 이미 있음). `03_seed.sql`.
3. DB 쓰기 역할 `writer_2b` 추가 (`04_roles.sql`, 테이블 단위 GRANT).
4. API에 scope `write:2b` 추가: `api/app/auth.py`의 `WRITE_SCOPES`, `api/app/db.py`의 `WRITER_FOR_LINE`, `api/deploy/install.sh`의 pg_hba/pg_ident 역할 목록, `routes_write.py`의 `Literal["2A", "PACK"]`.
5. `schema/test/`에 그 라인 smoke 시험 추가.
6. Grafana 대시보드 `grafana/2b/`.

## 5. 저장소를 고치는 사람·AI가 지킬 것

- **비밀은 저장소에 넣지 않는다:** 서버 IP, API 도메인, DB 비밀번호, API 키, SSH 개인 키. 문서에는 `<서버 IP>`, `<API 도메인>`으로 쓴다. (battery-db는 공개 저장소)
- **스키마 변경은 SQL 파일로만.** 서버에서 직접 `ALTER` 하고 끝내지 않는다. 여러 번 실행해도 안전하게 (`IF NOT EXISTS`, `CREATE OR REPLACE`).
- **이력은 지우지 않는다.** 상태가 바뀌면 행을 고치지 말고 검사·이벤트 행을 추가. 예외: `purpose = 'fake'` 데이터.
- **DB에는 이벤트만.** PLC 주기값(센서, 하트비트)은 쌓지 않는다. 실시간 값은 브리지/라이브 캐시에서.
- **한 테이블 = 한 쓰는 팀.** 다른 팀 테이블에 쓰기가 필요하면 그 팀과 합의 후 컬럼 단위 GRANT.
- **운영 DB에 시험하지 않는다.** 쓰기 시험은 `battery_test` 같은 별도 DB에서 하고 지운다 (`api/README.md` 시험 절).
- 단위: 전압 mV, 전류 mA, 내부저항 mΩ (정수). 시각 `timestamptz`, UTC 저장.
- 셀 ID `R<run 4자리>-<PLC 번호 6자리>`. PLC 번호는 run 안에서만 유일.

## 6. 정할 것

| 항목 | 누가 | 상태 |
|---|---|---|
| 이 문서의 방향 (쓰기는 API, Grafana 공통, 조작은 HMI) | 전 팀장 | 제안 |
| Grafana 운영 방식 A/B | SCADA 담당 (2A `01xooyeon`) + 2B 팀장 | 제안: 당장 A |
| 2A 메일박스 대역 (D2700~D2799)·ACK 방식·이벤트 종류 | 2A DB + PLC 담당 | 미정 |
| 2B가 DB에 쓸 것 (팩 라인 데이터를 읽기만 하는지, 따로 쓰는지) | 2B | 미정 |
| 팩 테이블 설계 확인 | 팩 팀 | 미정 |
| 셀·박스 ID 형식 (박스 제안 `B<run 4자리>-<등급><순번 3자리>`, 가짜 데이터가 이 형식) | 2A + 팩 | 미정 |
| 더 큰 서버 (A1 VM) | 관리자 | Oracle 용량 대기 |
