# battery-db API

팀원과 수집기는 DB 계정 대신 **API 키**로 읽고 쓴다. VM에서 DB 옆에 같이 돈다 (FastAPI, `127.0.0.1:8000`).

- 문서(Swagger): `/docs` — 엔드포인트, 파라미터, 응답 예시를 브라우저에서 바로 시험할 수 있다
- 단위: 전압 mV, 전류 mA, 내부저항 mΩ, 시각 UTC (ISO 8601)
- 인증: `X-API-Key: bdb_...` 또는 `Authorization: Bearer bdb_...` (`/health`만 인증 없음)

## API 키 받기 (팀원 본인)

GitHub 공개 키가 서버에 등록된 팀원(관리자가 `/etc/gh-tunnel-users`에 아이디 추가)은 직접 발급한다:

```powershell
ssh tunnel@<서버 IP> new      # 키 발급. 이미 있으면 이전 키는 바로 막히고 교체 (권한은 유지)
ssh tunnel@<서버 IP> info     # 내 키 정보
ssh tunnel@<서버 IP> revoke   # 내 키 폐기
```

- 키 이름 = GitHub 아이디. 처음 발급하면 `read`만. 쓰기 권한은 관리자가 `grant`로 준다.
- 키 원문은 발급할 때 한 번만 나온다. 잃어버리면 `new`로 다시 받는다.
- GitHub 목록에서 빠지면 5분 안에 키도 자동 폐기된다.
- 내 사용량: `GET /me`

## 접속

**HTTPS (기본):** `https://<API 도메인>/docs`. 도메인은 관리자에게 받는다 (서버 IP로 바로 풀리므로 저장소에 적지 않음).
Caddy가 Let's Encrypt 인증서를 자동 발급·갱신하고 `127.0.0.1:8000`으로 넘긴다 ([deploy/Caddyfile.example](deploy/Caddyfile.example)).

```powershell
curl.exe -H "X-API-Key: bdb_..." "https://<API 도메인>/runs?line=2A"
```

**SSH 터널 (예비):** `tunnel` 계정은 DB(5432)와 API(8000)만 포워딩된다 ([docs/server.md](../docs/server.md)).
`ssh -N -L 8000:localhost:8000 tunnel@<서버 IP>` 후 `http://localhost:8000/docs`.

## 엔드포인트

| 메서드 | 경로 | scope | 내용 |
|---|---|---|---|
| GET | `/health` | 없음 | DB까지 닿는지 |
| GET | `/me` | 아무 키 | 내 키 정보 + 사용량 (`v_api_usage`) |
| GET | `/stations?line=` | read | 스테이션 |
| GET | `/criteria/current` | read | 현재 판정 기준 |
| GET | `/runs?line=&limit=` | read | 시험 목록 (최신 순) |
| GET | `/runs/{run_id}` | read | 시험 + 수율(`v_run_yield`) + 세트 편차 비교(`v_set_spread_compare`) |
| GET | `/runs/{run_id}/testers` | read | 검사기 레인별 NG율·평균 검사 시간 |
| GET | `/runs/{run_id}/equipment` | read | 설비 상태별 누적 초 |
| GET | `/cells/{cell_id}` | read | 셀 + 검사 이력 + 최종 전압 판정 |
| GET | `/boxes?run_id=&status=&grade=` | read | 박스 + 세트 편차 |
| GET | `/boxes/{box_id}` | read | 박스 + 셀 4개 + 인계 |
| GET | `/lots` | read | LOT 보류·확정 NG |
| GET | `/alarms/open` | read | 열린 알람 |
| GET | `/collectors` | read | 수집기별 최신 상태, 누락 수, 끊김(stale) (`v_collector_status`) |
| GET | `/runs/{run_id}/gaps` | read | 빠진 이벤트 순번 구간 + 단계가 빠진 셀 (`v_event_gaps`, `v_cell_incomplete`) |
| GET | `/packs/{pack_id}/trace` | read | 팩 → 셀 → 2A 검사값 |
| POST | `/runs` | write:2a / write:pack | 시험 시작 → `run_id` |
| POST | `/runs/{run_id}/end` | 같은 라인 write | 시험 끝 |
| POST | `/events` | write:2a / write:pack | 이벤트 묶음 저장 (초안, 아래). `seq`는 `event_log.seq`에 저장 |
| POST | `/collector/health` | write:2a / write:pack | 수집기 상태 1건 (수집기가 30초마다) |

### `POST /events` (초안)

```json
{"events": [
  {"run_id": 12, "seq": 37, "source": "2A", "station_code": "2A-VIS1",
   "event_type": "cell.id_read", "entity_id": "R0012-000037",
   "payload": {"lot": "1"}, "occurred_at": "2026-11-23T14:03:05+09:00"}
]}
```

- 한 번에 최대 500건, 한 라인(`source`)만. 전부 들어가거나 전부 거절된다.
- **같은 이벤트를 다시 보내도 한 번만 저장된다.** `event_id`를 안 주면 `uuid5(NS, "run:<run_id>/seq:<seq>")`로 만든다
  (`NS = uuid5(NAMESPACE_URL, "https://github.com/intel7-team2/battery-db/event")`). 응답의 `duplicates`는 이미 있던 수.
- `occurred_at`은 시간대 필수. 스테이션은 같은 라인 것만.
- 지금은 원본 이벤트(`event_log`)만 쓴다. 이벤트를 `cell`·`cell_inspection` 등으로 옮기는 규칙은 PLC 메일박스·ACK 합의 뒤에 붙인다.

## API 키 (관리자, VM에서)

```bash
sudo battery-api-key add collector-2a --scope read --scope write:2a   # 프로그램용 키
sudo battery-api-key grant 01xooyeon write:2a                         # 팀원 키에 권한 추가 (키는 그대로)
sudo battery-api-key list            # 사용 중 (--all: 폐기된 것도)
sudo battery-api-key revoke 01xooyeon                                 # 다음 요청부터 막힘
```

키 원문은 발급할 때 한 번만 나온다 (파일에는 SHA-256만). 사람·프로그램마다 따로 쓴다.

## 추적 (누가 무엇을)

| 무엇 | 어디 |
|---|---|
| 요청 1건마다 (키 이름, 메서드, 경로, 결과, 걸린 시간, 시각) | `api_access_log` (2초마다 묶어서 기록) |
| 키별 요약 (요청 수, 쓰기, 오류, 최근 24시간, 처음·마지막 사용) | `v_api_usage`, `GET /me` |
| 어떤 키가 쓴 데이터인지 | `event_log.api_client`, `sim_run.api_client` |
| 실시간 로그 | `journalctl -u battery-api -f` (`<키 이름> GET /path 200 3ms`) |
| 접속 IP | Caddy 로그 `/var/log/caddy/api.log` (DB에는 안 남김) |

```sql
SELECT * FROM v_api_usage ORDER BY last_seen DESC;
```

## 구조

- API 서버는 DB에 비밀번호 없이 붙는다: OS 계정 `battery-api`가 유닉스 소켓 peer 인증으로 DB 역할 `reader` / `writer_2a` / `writer_pack` / `api_audit`(접근 로그만)에 매핑된다 (`pg_ident.conf`의 `batteryapi`). 키 scope에 따라 역할을 골라 쓰므로 `04_roles.sql`의 쓰기 분리가 그대로 적용된다.
- 키 셀프서비스: `tunnel` 계정에 명령을 붙여 접속하면 sshd가 `tunnel-session`만 실행한다 (`70-tunnel.conf` ForceCommand). 접속에 쓴 공개 키로 GitHub 아이디를 찾고, sudo로 허용된 명령 하나(`battery-api-self`)만 부른다. 쉘은 없다.
- 연결 풀: reader 최대 4, writer 각 2, api_audit 1. 메모리 상한 200 MB (실사용 약 40 MB).
- Grafana는 SQL 직결(`grafana` 계정)이 더 편해서 그대로 둔다.

## 설치·업데이트 (VM, root)

```bash
# PC에서: tar -cf - api schema | ssh ubuntu@<서버 IP> 'rm -rf /tmp/api-src && mkdir /tmp/api-src && tar -xf - -C /tmp/api-src'
sudo -u postgres psql -d battery -f /tmp/api-src/schema/05_api.sql        # 처음 한 번 (여러 번 돌려도 됨)
sudo -u postgres psql -d battery -f /tmp/api-src/schema/06_collector.sql  # API 0.3 전에 (event_log.seq, collector_health)
sudo bash /tmp/api-src/api/deploy/install.sh /tmp/api-src/api
```

`install.sh`는 여러 번 돌려도 된다: 서비스 계정, `/opt/battery-api`(코드 + venv), `/etc/battery-api/keys.json`, `battery-api-key` 명령, pg_hba/pg_ident 매핑, systemd `battery-api`, `tunnel` 계정(sshd 설정, 셀프서비스 명령, sudoers), GitHub 키 동기화 스크립트.

로그: `journalctl -u battery-api -f`

## 시험

`tests/api_smoke.py` (41개 항목: 인증, 조회, scope, 중복 제거, 검증, 추적). 쓰기 시험이 데이터를 남기므로 **시험용 DB**에 띄운 API에만 돌린다.
11/23: `battery_test`에 스키마 + smoke.sql(COMMIT) 넣고 :8001에 띄워 전부 통과, 시험 DB는 지움.
셀프서비스는 임시 키·가짜 GitHub 아이디로 시험: 발급, 교체(이전 키 401), 본인 폐기, 쉘 시도 차단, `-N` 포워딩 유지, 목록에서 빠지면 자동 폐기. 시험 흔적은 지움.

## 남은 것

- [x] HTTPS 공개 (11/23): Vercel DNS A 레코드 → 서버, Oracle 보안 목록·VM iptables TCP 80/443, Caddy. DB 5432는 계속 닫힘
- [ ] 이벤트 → 셀·검사 테이블 반영 규칙 (PLC 합의 뒤)
- [x] 키 셀프서비스 + 사용 추적 (11/23)
- [ ] 요청 수 제한, 접근 로그 보관 기간
