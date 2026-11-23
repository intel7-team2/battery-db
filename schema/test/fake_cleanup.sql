-- fake_run.sql 로 넣은 가짜 데이터 전부 삭제 (sim_run.purpose = 'fake' 인 run만). 실제 데이터는 건드리지 않는다.
--   sudo -u postgres psql -d battery -c 'SET ROLE battery' -f schema/test/fake_cleanup.sql

BEGIN;

CREATE TEMP TABLE fake_runs ON COMMIT DROP AS SELECT run_id FROM sim_run WHERE purpose = 'fake';
CREATE TEMP TABLE fake_lots ON COMMIT DROP AS
    SELECT l.lot_code FROM lot l JOIN fake_runs f ON l.lot_code LIKE 'F' || f.run_id || '-%';

DELETE FROM supply_handoff  WHERE box_id IN (SELECT box_id FROM cell_box WHERE run_id IN (SELECT run_id FROM fake_runs));
DELETE FROM cell_inspection WHERE cell_id IN (SELECT cell_id FROM cell WHERE run_id IN (SELECT run_id FROM fake_runs));
DELETE FROM event_log       WHERE run_id IN (SELECT run_id FROM fake_runs);
DELETE FROM cell            WHERE run_id IN (SELECT run_id FROM fake_runs);
DELETE FROM cell_box        WHERE run_id IN (SELECT run_id FROM fake_runs);
DELETE FROM lot_hold        WHERE lot_code IN (SELECT lot_code FROM fake_lots);
DELETE FROM lot             WHERE lot_code IN (SELECT lot_code FROM fake_lots);
DELETE FROM equipment_state WHERE run_id IN (SELECT run_id FROM fake_runs);
DELETE FROM alarm           WHERE run_id IN (SELECT run_id FROM fake_runs);
DELETE FROM sim_run         WHERE run_id IN (SELECT run_id FROM fake_runs);

COMMIT;
