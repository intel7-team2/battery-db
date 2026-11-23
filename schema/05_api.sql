-- API 사용 추적. 04_roles.sql 다음에 관리자(postgres)가 실행: sudo -u postgres psql -d battery -f 05_api.sql
-- 여러 번 실행해도 된다.
--
--   누가 썼나: event_log.api_client, sim_run.api_client = API 키 이름 (팀원은 GitHub 아이디)
--   누가 불렀나: api_access_log (요청 1건 = 1행), 요약은 v_api_usage

BEGIN;

ALTER TABLE event_log ADD COLUMN IF NOT EXISTS api_client text;  -- API로 들어온 행만. 직접 SQL이면 null
ALTER TABLE sim_run   ADD COLUMN IF NOT EXISTS api_client text;

CREATE TABLE IF NOT EXISTS api_access_log (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at      timestamptz NOT NULL DEFAULT now(),
    client  text,                      -- API 키 이름. 키 없이/잘못된 키면 null
    method  text NOT NULL,
    path    text NOT NULL,
    status  smallint NOT NULL,
    ms      integer NOT NULL
);
CREATE INDEX IF NOT EXISTS api_access_log_client_idx ON api_access_log (client, at);
ALTER TABLE api_access_log OWNER TO battery;

CREATE OR REPLACE VIEW v_api_usage AS
SELECT client,
       count(*)                                         AS requests,
       count(*) FILTER (WHERE method <> 'GET')          AS writes,
       count(*) FILTER (WHERE status >= 400)            AS errors,
       count(*) FILTER (WHERE at > now() - interval '24 hours') AS requests_24h,
       min(at)                                          AS first_seen,
       max(at)                                          AS last_seen
FROM api_access_log
GROUP BY client;
ALTER VIEW v_api_usage OWNER TO battery;

-- API 서버가 접근 로그만 쓰는 역할 (peer 인증, 비밀번호 없음)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'api_audit') THEN CREATE ROLE api_audit LOGIN; END IF;
END $$;
GRANT USAGE ON SCHEMA public TO api_audit;
GRANT INSERT ON api_access_log TO api_audit;
GRANT USAGE ON SEQUENCE api_access_log_id_seq TO api_audit;

GRANT SELECT ON api_access_log, v_api_usage TO reader, writer_2a, writer_pack;

COMMIT;
