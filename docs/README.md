# battery-db

인텔 과정 B 프로젝트 전체가 같이 쓰는 **공통 DB** 저장소. 셀이 전공정에 들어온 순간부터
팩으로 조립돼 분류될 때까지의 이력을 한 곳에 남긴다.

> 전체 구조·규칙·제안(미확정)은 [architecture-proposal.md](architecture-proposal.md), AI 에이전트용 요약은 [AGENTS.md](../AGENTS.md).

SQL 스키마는 [`schema/`](../schema/)에 있다 (11/23 초안, 팀 합의 전). 빈 DB에 순서대로 실행한다:

```powershell
psql -h localhost -p 5433 -U battery -d battery -f schema/01_tables.sql
psql -h localhost -p 5433 -U battery -d battery -f schema/02_views.sql
psql -h localhost -p 5433 -U battery -d battery -f schema/03_seed.sql
# 04_roles.sql, 05_api.sql은 관리자가 VM에서 postgres 계정으로
psql -h localhost -p 5433 -U battery -d battery -f schema/test/smoke.sql   # 데이터 안 남김 (ROLLBACK)
# 화면 개발용 가짜 운전 1회 (purpose='fake', 셀 100개): schema/test/fake_run.sql / 지우기: fake_cleanup.sql
```

팀원·수집기는 DB 계정 대신 **API**([`api/`](../api/README.md), API 키 인증)로 읽고 쓰는 것을 기본으로 한다. Grafana만 SQL 직결.

## 쓰는 쪽

| 저장소 | 이 DB에 쓰는 것 |
|---|---|
| `digital-twin-a` | 셀, LOT, 셀 검사, LOT 보류, 셀 박스, 공급 인계 |
| `pack-production` | 주문·작업, 팔레트, 팩, 팩 슬롯별 셀, 조립 비전·전압 검사, 재작업 |
| `digital-twin-b` | 미정 (팩 라인 데이터를 읽기만 하는지, 따로 쓰는지 협의) |

## 기준

- DB: **PostgreSQL**
- 테이블마다 **쓰는 쪽은 하나**. 다른 쪽은 읽기만. 2A interface.md의 PLC 대역 규칙과 같은 원칙.
- 이력은 지우지 않는다. 상태가 바뀌면 행을 고치는 대신 검사·이벤트 행을 추가한다.
- DB에는 **이벤트만** 쓴다 (셀 검사 끝, 슬롯 채움 등). PLC를 주기적으로 읽은 값(센서, 하트비트)을 그대로 쌓지 않는다.

## 호스팅 후보 (11/20 검토)

무료 클라우드 DB로 충분한지 따져 본 결과. 한도 수치는 11/20 기준 각 사 문서.

### 예상 사용량

| 항목 | 추정 | 근거 |
|---|---|---|
| 셀 1개당 쓰기 | 약 30행 | 셀·검사 2회·상태 변경 4회·event_log 6행, 인덱스 쓰기 포함 ×2 |
| 팩 1개당 쓰기 | 약 60행 | 슬롯 6·검사 2·상태 변경·event_log, 인덱스 포함 |
| 시연 1회 (셀 100개, 팩 25개) | 약 5,000행 쓰기 | 2A 발표 목표 100셀 기준 |
| 전체 저장량 | 수십 MB 이하 | 셀 1만 개 × 약 10 KB로 잡아도 100 MB |

데이터 양은 어느 무료 플랜이든 남는다. 문제는 양이 아니라 **쓰는 방식과 연결 방식**이다.

### 후보 비교

팀 방향: 수업 시간에 로컬 PC에 띄우는 것도 되지만 **클라우드를 선호**한다.
수치는 각 사 문서와 정리 글 기준이며, 정리 글끼리 숫자가 다른 곳은 공식 페이지에서 다시 확인해야 한다.

**PostgreSQL 계열 (이 설계를 그대로 씀)**

| 후보 | 무료 한도 | 쉬면 어떻게 되나 | 이 프로젝트에서 |
|---|---|---|---|
| **Neon** (Vercel Postgres도 Neon) | 프로젝트당 1 GB, 월 100 CU-시간 | 5분 쉬면 자동 정지, 다음 쿼리에 자동으로 깨어남 (첫 쿼리가 조금 느림) | 1순위. 브랜치 기능으로 시험 데이터 DB를 따로 떼기 쉬움 |
| **Supabase** | DB 500 MB, 프로젝트 2개 | 7일 동안 안 쓰면 일시 정지, 대시보드에서 손으로 다시 켜야 함 | 2순위. 테이블마다 REST API와 실시간 구독이 자동으로 생겨 웹 주문 화면·대시보드 만들기 편함 |
| **Aiven** | 1 GB, 1 CPU, 1 GB RAM, 단일 노드 | 항상 켜져 있음 (오래 안 쓰면 정지된다고 함) | 정지·재시작 걱정이 적음. 백업 조건이 불분명 |
| Prisma Postgres | 무료 상한 있음 (수치 확인 못 함), 백업 없음 | 다음 쿼리에 자동으로 깨어남 | 한도 숫자를 확인한 뒤 판단 |
| Render | 1 GB | **30일 뒤 만료**, 14일 유예 후 데이터 삭제 | 제외 |
| CockroachDB Basic | 월 약 5천만 RU, 10 GiB | | PostgreSQL과 완전히 같지 않음. 신규 가입이 무료 플랜인지 체험판인지 불분명 |

**SQLite·MySQL 계열 (설계를 고쳐야 함)**

| 후보 | 무료 한도 | 이 프로젝트에서 |
|---|---|---|
| Cloudflare D1 | 계정 5 GB (DB당 500 MB), 하루 쓰기 10만 행, 하루 읽기 500만 행 | 수집기가 Worker나 HTTP API를 거쳐야 함. 1초에 1행씩만 써도 하루 한도에 걸림 |
| Turso | 5 GB, 월 쓰기 1천만 행, 월 읽기 5억 행 | D1보다 한도가 넉넉하고 파이썬 클라이언트가 있음. 다만 SQLite라 jsonb, timestamptz 같은 형식을 바꿔야 함 |
| TiDB Cloud Starter | 25 GiB, 월 2.5억 RU | MySQL 호환. PostgreSQL 호환은 아직 미리보기 |

**로컬 (예비)**

| 후보 | 이 프로젝트에서 |
|---|---|
| 팀 PC에 PostgreSQL | 수업 시간에 띄우면 됨. 네트워크 걱정이 없음. 클라우드와 같은 스키마라 `pg_dump`로 옮길 수 있음 |

### 결정 (11/20): Oracle Cloud 무료 VM에 PostgreSQL 직접 설치

시연만 보면 무료 관리형 DB로 충분하다. 하지만 **개발 중 확인과 시뮬레이션용 가상 데이터**까지 쌓으면 한도에 걸린다.
셀 10만 개로 시뮬레이션하면 1회에 쓰기 약 300만 행, 저장량 약 1 GB다. 이 정도면 Supabase는 한 번에 저장 한도를 넘고, Neon은 한도에 거의 닿고, Turso는 한 달에 2~3번만 돌릴 수 있다.
그래서 행 수·컴퓨트 한도가 없는 VM에 PostgreSQL을 직접 올린다.

| 항목 | 내용 |
|---|---|
| 어디 | Oracle Cloud Always Free, Ampere A1 (ARM) VM 1대 |
| 사양 | **2 OCPU / 12 GB RAM** (월 1,500 OCPU-시간, 9,000 GB-시간), 부트+블록 스토리지 합계 200 GB (부트 최소 47~50 GB), 볼륨 백업 5개, Object Storage 20 GB, 월 송신 10 TB. 공식 문서 11/20 확인 |
| DB | PostgreSQL. schema.md 설계를 그대로 씀 |
| 용도 | 팀 공용 DB: 개발 확인, 시뮬레이션 데이터, 통합 시험, 실제 라인, 시연 |
| 장점 | 항상 켜져 있음. 내 PC를 켜 둘 필요 없음. 포트를 직접 정할 수 있음 (실습실에서 5432가 막히면 다른 포트로) |

**가능 여부 확인 (11/20, 공식 문서 + 사용자 보고)**

| 항목 | 결과 | 영향 |
|---|---|---|
| 사양 | 2 OCPU / 12 GB | 이 프로젝트 DB와 시뮬레이션에 충분 |
| 리전 | 무료 VM은 **홈 리전**에만 만들 수 있음. A1은 **춘천(South Korea North) 불가** | 가입 후에는 홈 리전을 바꿀 수 없음. 서울은 가입 때 선택할 수 없어서 **오사카**로 함 |
| 자원 부족 | A1이 "out of host capacity"로 안 만들어질 수 있음 (공식 문서에 명시) | 다른 가용 영역, 나중에 재시도, Pay As You Go 전환. 안 되면 AMD Micro(1 GB RAM) 2대로 임시 운영 |
| 유휴 회수 | 7일 동안 CPU(95퍼센타일)·네트워크·메모리(A1만) 사용률이 모두 20% 미만이면 회수될 수 있음 | DB 서버는 평소 부하가 낮아서 걸리기 쉬움 |
| Pay As You Go 면제 | **공식 문서에는 없음.** 전환 후 회수가 멈췄다는 사용자 보고만 있음 | 전환하더라도 백업을 따로 두는 걸 전제로 함 |
| 가입 | 카드 인증 필요 | 계정 주인이 직접 가입 |

결론: **가능하다.** 조건은 세 가지다. 홈 리전(오사카)에 만들 것, A1 자원이 없으면 재시도하거나 Pay As You Go로 전환할 것, 회수에 대비해 백업을 VM 밖에 둘 것.

**유휴 회수 대응**

- Pay As You Go로 전환하고 예산 알림을 건다. Always Free 한도 안의 자원은 계속 무료다.
- 매일 백업을 Object Storage나 팀 드라이브에 두고, VM을 다시 만드는 순서를 문서로 남긴다.

**운영 규칙**

- 백업: 매일 `pg_dump` 결과를 Object Storage(Always Free 포함)나 팀 드라이브로 옮긴다.
- 접속: DB 포트는 팀원 IP 대역이나 VPN(Tailscale 등)에만 연다. 인터넷 전체에 열지 않는다.
- 계정: 라인별 쓰기 계정을 나눈다 (`writer_2a`, `writer_pack`, 읽기 전용 `reader`).
- 시뮬레이션 데이터는 `source = 'SIM'`으로 구분하고, 대량으로 넣을 때는 `COPY`를 쓴다.
- **인터넷이 끊겨도 라인은 돌아야 한다.** 수집기는 로컬에 먼저 기록하고 같은 event_id로 다시 보낸다 (2A design-notes의 "DB 저장 실패" 규칙).
- 예비: 각자 PC에 같은 스키마로 PostgreSQL(Docker)을 띄울 수 있게 해 둔다.

### 확인할 것

- [x] Oracle 가입. 서울은 가입 때 선택할 수 없어서 홈 리전이 **오사카(ap-osaka-1)**로 됨 (11/20). 한국에서 약 30 ms
- [x] 네트워크: VCN `battery-vcn`, 인터넷 게이트웨이, 공개 서브넷 `battery-public` (11/20, OCI CLI로 생성)
- [ ] 오사카 A1 VM: 11/20 "Out of capacity". 자원이 날 때까지 2분마다 자동 재시도 중 (2/12와 1/6 번갈아). 11/22 현재도 자원 부족, 1/6으로 재시도 중
- [x] 임시 DB 서버: AMD Micro VM에 PostgreSQL 16, 예약 IP, 6시간 백업, SSH 터널 + Tailscale 예비 (11/21~22). [server.md](server.md)
- [x] 현재 A1 무료 사양: 2 OCPU / 12 GB (11/20 공식 문서)
- [ ] Pay As You Go 전환 여부와 예산 알림
- [ ] 실습실 네트워크에서 VM으로 SSH(22번)가 되는지. DB 포트는 인터넷에 열지 않음
- [ ] 서버 관리 담당자

## 문서

```
docs/
  README.md    이 문서
  schema.md    ERD, 테이블 정의, 상태 코드, 미정 항목 (11/20 초안)
  server.md    DB 서버 구성, 접속, 백업, 성능 (11/22)
```
