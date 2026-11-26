-- 수집기 데이터 누락을 DB에서 보이게. 05_api.sql 다음에 관리자(postgres)가 실행:
--   sudo -u postgres psql -d battery -f 06_collector.sql
-- 여러 번 실행해도 된다.
--
--   event_log.seq       수집기 순번 (run 안에서 계속 증가). 빠진 번호 = DB에 안 들어온 이벤트
--   collector_health    수집기가 30초마다 보내는 상태 (PLC 넘침, 잃은 레코드, 밀린 전송)
--   v_event_gaps        run별 빠진 순번 구간
--   v_cell_incomplete   단계가 빠진 셀 (래더가 이벤트를 안 올린 경우까지 잡는 유일한 방법)
--   v_collector_status  수집기별 최신 상태 + 끊김 여부

BEGIN;

-- ── 순번 ──
ALTER TABLE event_log ADD COLUMN IF NOT EXISTS seq bigint;   -- API가 run_id + seq로 받은 값. 직접 SQL·예전 행은 null
CREATE INDEX IF NOT EXISTS event_log_run_seq_idx ON event_log (run_id, seq) WHERE seq IS NOT NULL;

-- run 안에서 순번이 비는 구간. 시작 전 구간은 모른다 (수집기는 PLC가 가리키는 곳부터 시작).
-- PLC가 넘침으로 버린 이벤트는 순번을 받지 못하므로 여기 안 나온다 → collector_health.plc_overflow
CREATE OR REPLACE VIEW v_event_gaps AS
WITH s AS (
    SELECT run_id, source, seq, occurred_at,
           lead(seq)         OVER w AS next_seq,
           lead(occurred_at) OVER w AS next_at
    FROM event_log
    WHERE seq IS NOT NULL AND run_id IS NOT NULL
    WINDOW w AS (PARTITION BY run_id, source ORDER BY seq)
)
SELECT s.run_id, s.source, r.purpose,
       s.seq + 1           AS gap_from,
       s.next_seq - 1      AS gap_to,
       s.next_seq - s.seq - 1 AS missing,
       s.occurred_at       AS before_at,
       s.next_at           AS after_at
FROM s JOIN sim_run r USING (run_id)
WHERE s.next_seq - s.seq > 1;

-- 셀마다 이벤트 단계 확인. 끝(packed/scrapped)까지 못 갔거나 앞 단계가 빠진 셀만.
-- 아직 라인 위에 있는 셀을 빼려고: run이 끝났거나 마지막 이벤트 뒤 10분이 지난 셀만 본다.
-- 이벤트 이름은 collector/config/2a.toml 가정값 기준 (PLC 합의 후 바뀌면 여기도)
CREATE OR REPLACE VIEW v_cell_incomplete AS
WITH c AS (
    SELECT run_id, entity_id AS cell_id,
           count(*)                                                     AS events,
           min(occurred_at)                                             AS first_at,
           max(occurred_at)                                             AS last_at,
           (array_agg(event_type ORDER BY occurred_at DESC, seq DESC NULLS LAST))[1] AS last_event,
           bool_or(event_type = 'cell.created')                         AS has_created,
           bool_or(event_type = 'cell.id_read')                         AS has_id_read,
           bool_or(event_type = 'cell.test_end')                        AS has_test_end,
           bool_or(event_type = 'cell.scrapped')                        AS scrapped,
           bool_or(event_type = 'cell.packed')                          AS packed
    FROM event_log
    WHERE event_type LIKE 'cell.%' AND entity_id IS NOT NULL AND run_id IS NOT NULL
    GROUP BY run_id, entity_id
)
SELECT c.run_id, r.purpose, c.cell_id, c.events, c.first_at, c.last_at, c.last_event,
       array_remove(ARRAY[
           CASE WHEN NOT c.has_created THEN 'created' END,
           CASE WHEN NOT c.has_id_read THEN 'id_read' END,
           CASE WHEN c.packed AND NOT c.has_test_end THEN 'test_end' END,
           CASE WHEN NOT (c.packed OR c.scrapped) THEN 'packed_or_scrapped' END
       ], NULL) AS missing
FROM c JOIN sim_run r USING (run_id)
WHERE (NOT c.has_created OR NOT c.has_id_read OR (c.packed AND NOT c.has_test_end)
       OR NOT (c.packed OR c.scrapped))
  AND (r.ended_at IS NOT NULL OR c.last_at < now() - interval '10 minutes');

-- ── 수집기 상태 ──
CREATE TABLE IF NOT EXISTS collector_health (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    line            text NOT NULL CHECK (line IN ('2A', 'PACK', '2B')),
    run_id          integer REFERENCES sim_run,
    api_client      text,                    -- 보낸 API 키 이름
    reported_at     timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,             -- 수집기 프로세스 시작 (아래 수는 이때부터 누적)
    last_poll_at    timestamptz,             -- 마지막으로 PLC를 읽은 시각
    polls           bigint  NOT NULL DEFAULT 0,
    events          bigint  NOT NULL DEFAULT 0,
    lost            integer NOT NULL DEFAULT 0,   -- 수집기가 늦어 덮어써진 레코드
    torn_skipped    integer NOT NULL DEFAULT 0,   -- 끝내 안 맞아 건너뛴 레코드
    unmapped        integer NOT NULL DEFAULT 0,   -- 설정에 없는 스테이션 (로컬 dead)
    resyncs         integer NOT NULL DEFAULT 0,   -- PLC 재시작 감지
    plc_overflow    integer NOT NULL DEFAULT 0,   -- PLC가 넘침으로 버린 수 (D2702)
    outbox_pending  integer NOT NULL DEFAULT 0,   -- 아직 못 보낸 이벤트
    outbox_dead     integer NOT NULL DEFAULT 0,   -- API가 거절했거나 매핑 못 한 이벤트
    upload_backoff_s real,                    -- 0이 아니면 전송 실패 중
    detail          jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS collector_health_idx ON collector_health (line, api_client, reported_at DESC);
ALTER TABLE collector_health OWNER TO battery;

-- 수집기(라인 + 키)마다 최신 1행. 90초 넘게 소식이 없으면 stale (보고 주기 30초)
CREATE OR REPLACE VIEW v_collector_status AS
SELECT DISTINCT ON (line, api_client)
       line, api_client, run_id, reported_at, last_poll_at,
       now() - reported_at                AS since_report,
       reported_at < now() - interval '90 seconds' AS stale,
       events, lost, torn_skipped, unmapped, resyncs, plc_overflow,
       lost + torn_skipped + plc_overflow AS dropped_total,
       outbox_pending, outbox_dead, upload_backoff_s
FROM collector_health
ORDER BY line, api_client, reported_at DESC;
ALTER VIEW v_collector_status OWNER TO battery;
ALTER VIEW v_event_gaps OWNER TO battery;
ALTER VIEW v_cell_incomplete OWNER TO battery;

GRANT INSERT ON collector_health TO writer_2a, writer_pack;
GRANT USAGE ON SEQUENCE collector_health_id_seq TO writer_2a, writer_pack;
GRANT SELECT ON collector_health, v_collector_status, v_event_gaps, v_cell_incomplete
    TO reader, writer_2a, writer_pack;

COMMIT;
