-- 라인별 쓰기 권한 분리. "테이블마다 쓰는 쪽은 하나" 규칙을 DB가 강제한다.
-- 관리자(postgres)가 VM에서 한 번 실행: sudo -u postgres psql -d battery -f 04_roles.sql
-- 비밀번호는 여기 적지 않는다. 실행 뒤 \password writer_2a 등으로 따로 설정.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reader')      THEN CREATE ROLE reader      LOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'writer_2a')   THEN CREATE ROLE writer_2a   LOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'writer_pack') THEN CREATE ROLE writer_pack LOGIN; END IF;
    -- Grafana(SCADA) 전용. reader 권한을 물려받고, 사람이 쓰는 reader와 따로 끊거나 비밀번호를 바꿀 수 있다
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana')     THEN CREATE ROLE grafana     LOGIN IN ROLE reader; END IF;
END $$;

-- 대시보드가 무거운 쿼리로 DB(1 GB Micro)를 잡지 않게
ALTER ROLE grafana CONNECTION LIMIT 5;
ALTER ROLE grafana SET default_transaction_read_only = on;
ALTER ROLE grafana SET statement_timeout = '10s';

-- 모두 읽기
GRANT USAGE ON SCHEMA public TO reader, writer_2a, writer_pack;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO reader, writer_2a, writer_pack;

-- 공통: 각 라인이 자기 행을 추가 (source/line 컬럼으로 구분)
GRANT INSERT ON event_log, equipment_state, alarm, sim_run TO writer_2a, writer_pack;
GRANT UPDATE (ended_at)                         ON equipment_state TO writer_2a, writer_pack;
GRANT UPDATE (acked_at, acked_by, cleared_at)   ON alarm           TO writer_2a, writer_pack;
GRANT UPDATE (ended_at, note)                   ON sim_run         TO writer_2a, writer_pack;

-- 2A
GRANT INSERT, UPDATE ON lot, lot_hold, criteria_set, cell_box, cell, cell_inspection TO writer_2a;
GRANT INSERT, UPDATE ON supply_handoff TO writer_2a;

-- 팩 라인: 팩 테이블 + 셀이 팩에 들어갔을 때 상태 변경, 박스 소비, 인계 수신 확인
GRANT INSERT, UPDATE ON customer_order, work_order, pallet, pack_recipe, recipe_slot,
                        pack, pack_slot, pack_inspection, rework TO writer_pack;
GRANT UPDATE (status)                       ON cell, cell_box  TO writer_pack;
GRANT UPDATE (confirmed_at, status)         ON supply_handoff  TO writer_pack;

-- IDENTITY 컬럼 시퀀스
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO writer_2a, writer_pack;

COMMIT;
