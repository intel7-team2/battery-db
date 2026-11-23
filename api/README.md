# battery-db API

팀원과 수집기는 DB 계정 대신 **API 키**로 읽고 쓴다. VM에서 DB 옆에 같이 돈다 (FastAPI, `127.0.0.1:8000`).

- 문서(Swagger): `/docs` — 엔드포인트, 파라미터, 응답 예시를 브라우저에서 바로 시험할 수 있다
- 단위: 전압 mV, 전류 mA, 내부저항 mΩ, 시각 UTC (ISO 8601)
- 인증: `X-API-Key: bdb_...` 또는 `Authorization: Bearer bdb_...` (`/health`만 인증 없음)

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
| GET | `/packs/{pack_id}/trace` | read | 팩 → 셀 → 2A 검사값 |
| POST | `/runs` | write:2a / write:pack | 시험 시작 → `run_id` |
| POST | `/runs/{run_id}/end` | 같은 라인 write | 시험 끝 |
| POST | `/events` | write:2a / write:pack | 이벤트 묶음 저장 (초안, 아래) |

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
sudo battery-api-key add 01xooyeon --scope read                  # 조회만
sudo battery-api-key add collector-2a --scope read --scope write:2a
sudo battery-api-key list
sudo battery-api-key revoke 01xooyeon                            # 다음 요청부터 막힘
```

키 원문은 발급할 때 한 번만 나온다 (파일에는 SHA-256만). 사람·프로그램마다 따로 발급한다.

## 구조

- API 서버는 DB에 비밀번호 없이 붙는다: OS 계정 `battery-api`가 유닉스 소켓 peer 인증으로 DB 역할 `reader` / `writer_2a` / `writer_pack`에 매핑된다 (`pg_ident.conf`의 `batteryapi`). 키 scope에 따라 역할을 골라 쓰므로 `04_roles.sql`의 쓰기 분리가 그대로 적용된다.
- 연결 풀: reader 최대 4, writer 각 2. 메모리 상한 200 MB (실사용 약 40 MB).
- Grafana는 SQL 직결(`grafana` 계정)이 더 편해서 그대로 둔다.

## 설치·업데이트 (VM, root)

```bash
# PC에서: tar -cf - api | ssh ubuntu@<서버 IP> 'rm -rf /tmp/api-src && mkdir /tmp/api-src && tar -xf - -C /tmp/api-src'
sudo bash /tmp/api-src/api/deploy/install.sh /tmp/api-src/api
```

`install.sh`는 여러 번 돌려도 된다: 서비스 계정, `/opt/battery-api`(코드 + venv), `/etc/battery-api/keys.json`, `battery-api-key` 명령, pg_hba/pg_ident 매핑, systemd `battery-api`.

로그: `journalctl -u battery-api -f`

## 시험

`tests/api_smoke.py` (36개 항목: 인증, 조회, scope, 중복 제거, 검증). 쓰기 시험이 데이터를 남기므로 **시험용 DB**에 띄운 API에만 돌린다.
11/23: `battery_test`에 스키마 + smoke.sql(COMMIT) 넣고 :8001에 띄워 전부 통과, 시험 DB는 지움.

## 남은 것

- [x] HTTPS 공개 (11/23): Vercel DNS A 레코드 → 서버, Oracle 보안 목록·VM iptables TCP 80/443, Caddy. DB 5432는 계속 닫힘
- [ ] 이벤트 → 셀·검사 테이블 반영 규칙 (PLC 합의 뒤)
- [ ] 요청 수 제한, 접근 로그 보관
