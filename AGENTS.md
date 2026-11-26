# battery-db — AI 에이전트용 안내

인텔 과정 B 프로젝트 전 팀(2A 셀 전공정, 팩 라인, 2B)이 같이 쓰는 PostgreSQL DB와 그 API.
작업 전에 읽을 것:

1. [docs/architecture-proposal.md](docs/architecture-proposal.md) — 전체 구조, 현재 상태, **제안(미확정) 방향**, 지킬 규칙
2. [docs/server.md](docs/server.md) — 서버·접속·계정·백업
3. [api/README.md](api/README.md) — API, 키, 추적, 배포, 시험
4. [schema/](schema/) — `01`~`06` 번호 순서대로 적용하는 SQL
5. [collector/README.md](collector/README.md) — PLC 메일박스 → API 수집기 (메일박스 배치는 `collector/config/2a.toml`)

## 꼭 지킬 것

- **비밀 금지:** 서버 IP, API 도메인, 비밀번호, API 키, SSH 키를 파일·커밋·PR·이슈에 쓰지 않는다. 공개 저장소다. 문서에는 `<서버 IP>`, `<API 도메인>`.
- **스키마는 SQL 파일로만 바꾼다.** 다시 실행해도 안전하게 (`IF NOT EXISTS`, `CREATE OR REPLACE`). 운영 DB에서 직접 바꾸고 끝내지 않는다.
- **한 테이블은 한 팀만 쓴다.** 다른 팀 테이블은 읽기만. 권한은 `04_roles.sql`, API scope는 `api/app/auth.py`.
- **이력은 지우지 않는다** (행 추가로 상태 변경). 예외: `sim_run.purpose = 'fake'` 데이터.
- **운영 DB에 쓰기 시험 금지.** 별도 DB(`battery_test`)에 띄워서 시험하고 지운다.
- 단위: mV / mA / mΩ 정수, 시각 `timestamptz` UTC.
- `docs/architecture-proposal.md`의 "제안" 부분은 아직 팀 결정이 아니다. 그 방향과 다르게 구현해야 하면 사람에게 먼저 확인한다.

## 자주 하는 일

| 하려는 것 | 어디 |
|---|---|
| 뷰·지표 추가 | `schema/02_views.sql` (Grafana 패널에 긴 SQL을 쓰기보다 뷰로) |
| 새 라인(예: 2B) 붙이기 | architecture-proposal.md §4 |
| API 엔드포인트 추가 | `api/app/routes_read.py` / `routes_write.py`, 시험 `api/tests/api_smoke.py` |
| 서버에 배포 | `api/README.md` "설치·업데이트" (관리자 권한 필요) |
| 수집기 메일박스 배치 바꾸기 | `collector/config/2a.toml`만 (코드는 그대로). 시험 `collector/tests/` |
| 화면용 가짜 데이터 | `schema/test/fake_run.sql`, 지우기 `fake_cleanup.sql` |
