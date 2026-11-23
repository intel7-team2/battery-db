# DB 서버 운영 (11/22 기준)

A1 VM이 아직 안 만들어져서, 같은 오사카 리전에 **AMD Micro VM 1대**로 먼저 운영한다.
A1이 생기면 `pg_dump`로 옮긴다.

공개 저장소라서 서버 주소(IP)는 여기 적지 않는다. 관리자에게 받는다.

## 구성

| 항목 | 내용 |
|---|---|
| VM | `battery-db-micro`, VM.Standard.E2.1.Micro (Always Free), Ubuntu 24.04 Minimal |
| 메모리 | 1 GB + 스왑 2 GB |
| 디스크 | 45 GB (사용 10%) |
| 공인 IP | 예약 IP (VM을 재시작해도 안 바뀜) |
| DB | PostgreSQL 16, DB `battery`, 소유 계정 `battery` |
| 포트 | **5432는 VM 안(localhost)에서만 열림.** 인터넷에 열지 않고 SSH 터널로 접속 |
| 설정 | 1 GB 메모리에 맞춤: `shared_buffers=192MB`, `max_connections=30`, `work_mem=4MB` |

## 접속

### 1. SSH 터널 (기본)

| 누구 | SSH 계정 | 할 수 있는 것 | 허용 목록 |
|---|---|---|---|
| 관리자 | `ubuntu` | 쉘, sudo, 터널 | `/etc/gh-key-users` |
| 팀원 | `tunnel` | **DB(5432)·API(8000) 포트 포워딩 + API 키 셀프서비스(`ssh tunnel@<서버 IP> new`).** 쉘·tty·다른 포트 막힘 (`/etc/ssh/sshd_config.d/70-tunnel.conf` + 키마다 `restrict,permitopen`) | `/etc/gh-tunnel-users` |

팀원:

```powershell
ssh -N -L 5433:localhost:5432 tunnel@<서버 IP>
```

(키 파일이 기본 위치 `~/.ssh/id_ed25519`가 아니면 `-i <키 파일>` 추가.) 아무것도 안 뜨고 멈춰 있으면 정상이다.
이 창을 켜 둔 동안 `localhost:5433`이 서버 DB로 연결된다. 앱, psql, DBeaver, Grafana 모두 `localhost:5433`, DB `battery`로 접속한다.

서버 주소와 DB 비밀번호는 저장소에 적지 않는다. 관리자에게 따로 받는다.

**API(권장):** DB 계정 대신 API 키로 읽고 쓴다. HTTPS `https://<API 도메인>/docs` (터널 불필요, 도메인은 관리자에게). 자세한 건 [api/README.md](../api/README.md).
인터넷에 열린 포트: 22(SSH 키), 80·443(Caddy → API). 5432는 닫혀 있다.
DB 터널과 API 터널을 한 번에: `ssh -N -L 5433:localhost:5432 -L 8000:localhost:8000 tunnel@<서버 IP>`.

### DB 계정

| 계정 | 용도 | 권한 |
|---|---|---|
| `reader` | 사람이 조회 (DBeaver, psql) | 모든 테이블·뷰 SELECT |
| `grafana` | Grafana SCADA 데이터 소스 | `reader`와 같음 + 읽기 전용 트랜잭션, 쿼리 10초 제한, 동시 연결 5개 |
| `writer_2a` | 2A 수집기 | 2A 테이블 쓰기 (`schema/04_roles.sql`) |
| `writer_pack` | 팩 라인 | 팩 테이블 쓰기 |
| `battery` | 관리자 (스키마 변경) | 소유자 |

Grafana PostgreSQL 데이터 소스: Host `localhost:5433` (Docker 안이면 `host.docker.internal:5433`), Database `battery`, User `grafana`, TLS `disable` (SSH 터널이 암호화), Version 16.

### 2. Tailscale (예비)

실습실 네트워크가 SSH(22번)를 막을 때 쓴다. VM이 관리자의 Tailscale 네트워크에 `battery-db`로 들어가 있다.
Tailscale을 설치하고 같은 네트워크에 들어간 PC에서:

```powershell
ssh -N -L 5433:localhost:5432 ubuntu@battery-db
```

관리자 본인의 기기는 Tailscale SSH로 인증하므로 SSH 키가 없어도 된다 (브라우저 로그인을 다시 요구할 수 있음).
팀원은 관리자가 VM을 공유해 줘야 하고, 이때는 SSH 키가 필요하다: `ssh -i <키 파일> -N -L 5433:localhost:5432 ubuntu@<Tailscale 이름>`. 팀원 경로는 아직 시험하지 않았다.

### SSH 키 등록

VM이 5분마다 **등록된 GitHub 계정의 공개 키**(`github.com/<아이디>.keys`)를 가져와 접속 허용 목록을 다시 만든다.

1. 내 PC에서 키를 만든다: `ssh-keygen -t ed25519`
2. `.pub` 파일 내용을 GitHub → Settings → SSH and GPG keys에 추가한다.
3. 관리자가 VM의 `/etc/gh-tunnel-users`(팀원) 또는 `/etc/gh-key-users`(관리자)에 GitHub 아이디를 한 줄 추가한다.
4. 5분 뒤 접속된다 (바로 하려면 `sudo systemctl start gh-keys.service`). GitHub에서 키를 지우거나 목록에서 빼면 접속도 막힌다.

동기화 스크립트: `/usr/local/bin/sync-gh-keys.sh` (`gh-keys.timer`, 5분마다, 원본 [api/deploy/sync-gh-keys.sh](../api/deploy/sync-gh-keys.sh)). 목록에서 빠진 팀원의 API 키도 같이 폐기한다.

GitHub 계정이 털리면 서버도 열린다. **GitHub 2단계 인증을 켤 것.**
GitHub에 접속이 안 될 때는 기존 목록을 그대로 둔다. 관리자 키 1개는 고정이라 지워지지 않는다.

## 백업

| 어디 | 주기 | 보관 |
|---|---|---|
| VM `/var/backups/postgres` | 6시간마다 (UTC 00/06/12/18시) `pg_dump -Fc` | 14일 |
| 관리자 PC | 6시간마다 + 로그인할 때 VM에서 복사 | 60일 |

최대 6시간치 데이터를 잃을 수 있다. 시험 단계에서는 충분하다.

복원 (터널을 연 PC에서): `pg_restore -h localhost -p 5433 -U battery -d battery --clean --if-exists <파일>.dump`

## 유휴 회수 대응

7일 동안 사용률이 낮으면 Oracle이 VM을 회수할 수 있다. 매시간 약 6분 동안 가장 낮은 우선순위로 CPU를 써서 막는다 (`keepalive.timer`). DB가 우선이라 영향은 작을 것으로 보지만 측정하지 않았다.

## 성능 (11/22 측정)

| 시험 | 결과 |
|---|---|
| VM 안, pgbench 기본 트랜잭션, 8개 동시 | 초당 약 830건, 평균 9.6 ms |
| VM 안, INSERT만, 4개 동시 | 초당 약 2,800건 |
| 한국 PC → 터널, **1행씩 INSERT** | **초당 약 22행** (왕복 약 43 ms) |
| 한국 PC → 터널, 1만 행을 한 문장으로 | 0.7초 미만 (초당 약 1.4만 행 이상) |

서버는 충분하다. 병목은 오사카 왕복 시간이다 (ping은 약 30 ms, SSH 터널 + DB 응답까지 약 43 ms).
**수집기와 시뮬레이터는 여러 행을 묶어서 보내야 한다** (multi-row `INSERT`, `COPY`, 또는 일정 시간마다 모아서 전송).
연결은 한 번 열어서 계속 쓴다. 앱이 쓸 수 있는 동시 연결은 약 27개다 (30개 중 3개는 관리용).

## 아직 안 된 것

- [x] 스키마 적용 (11/23, `schema/`)
- [x] 라인별 계정 생성 (11/23). 비밀번호는 관리자가 VM에서 `\password <계정>`으로 설정
- [ ] 실습실 네트워크에서 SSH(22번)가 되는지
- [x] 팀원 SSH: `tunnel` 계정 (11/23, 포워딩만 되는 것 확인)
- [ ] Tailscale 공유
- [ ] A1 VM: 11/22 현재도 "Out of capacity". 1 OCPU / 6 GB로 자동 재시도 중
