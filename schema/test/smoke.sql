-- 스모크 시험: 가상 셀 24개를 2A v2 흐름으로 흘리고 팩 1개까지 만든 뒤 뷰를 확인한다.
-- 전부 한 트랜잭션 안에서 하고 마지막에 ROLLBACK 하므로 실제 DB에 돌려도 데이터가 남지 않는다.
--   psql -h localhost -p 5433 -U battery -d battery -f schema/test/smoke.sql
-- 실패하면 RAISE EXCEPTION 으로 멈춘다.

BEGIN;

DO $$
DECLARE
    v_run   integer;
    v_crit  integer;
    n       integer;
    v_cell  text;
    v_lot   text;
    v_ocv   integer;
    v_ir    integer;
    v_grade char(1);
    v_res   smallint;
    v_t0    timestamptz := '2026-11-23 10:00:00+09';
    v_ts    timestamptz;
    v_box   text;
    v_slot  smallint;
    g       char(1);
BEGIN
    INSERT INTO sim_run (line, purpose, scene_version) VALUES ('2A', 'test', 'smoke') RETURNING run_id INTO v_run;
    SELECT id INTO v_crit FROM criteria_set ORDER BY valid_from DESC LIMIT 1;
    INSERT INTO lot (lot_code) VALUES ('1'), ('4'), ('7') ON CONFLICT DO NOTHING;

    FOR n IN 1..24 LOOP
        v_cell := format('R%s-%s', lpad(v_run::text, 4, '0'), lpad(n::text, 6, '0'));
        v_lot  := (ARRAY['1', '4', '7'])[(n % 3) + 1];
        v_ts   := v_t0 + make_interval(secs => n * 6);

        INSERT INTO cell (cell_id, lot_code, run_id, plc_no, created_at) VALUES (v_cell, v_lot, v_run, n, v_ts);
        INSERT INTO event_log (event_id, source, station_code, event_type, entity_id, run_id, occurred_at)
        VALUES (md5(v_run || ':' || n)::uuid, '2A', '2A-VIS1', 'cell.id_read', v_cell, v_run, v_ts);
        INSERT INTO cell_inspection (cell_id, kind, station_code, result_code, measured_at)
        VALUES (v_cell, 'id_read', '2A-VIS1', 0, v_ts);

        -- 외관: 5번 셀만 NG
        INSERT INTO cell_inspection (cell_id, kind, station_code, result_code, reason_code, measured_at)
        VALUES (v_cell, 'appearance', '2A-VIS2', CASE WHEN n = 5 THEN 9 ELSE 0 END,
                CASE WHEN n = 5 THEN 11 ELSE 0 END, v_ts + interval '2 s');
        IF n = 5 THEN
            UPDATE cell SET status = 'scrapped' WHERE cell_id = v_cell;
            CONTINUE;
        END IF;

        -- 1차 전압 검사: 레인 = n % 4 + 1. 7, 11번은 내부저항 초과
        v_ocv := 3560 + (n * 53) % 240;
        v_ir  := CASE WHEN n IN (7, 11) THEN 75 ELSE 25 + n % 15 END;
        v_grade := CASE WHEN v_ocv >= 3750 THEN 'A' WHEN v_ocv >= 3650 THEN 'B' ELSE 'C' END;
        v_res := CASE WHEN v_ir > 60 THEN 9 WHEN v_grade = 'A' THEN 1 WHEN v_grade = 'B' THEN 2 ELSE 3 END;
        INSERT INTO cell_inspection (cell_id, kind, station_code, attempt, ocv_mv, ir_mohm, result_code,
                                     reason_code, criteria_set_id, started_at, measured_at)
        VALUES (v_cell, 'electrical', '2A-T' || (n % 4 + 1), 1, v_ocv, v_ir, v_res,
                CASE WHEN v_res = 9 THEN 3 ELSE 0 END, v_crit, v_ts + interval '10 s', v_ts + interval '28 s');

        IF v_res = 9 THEN
            -- 재검: 7번은 회복, 11번은 다시 NG → 최종 배출
            UPDATE cell SET status = 'retest' WHERE cell_id = v_cell;
            v_ir  := CASE WHEN n = 7 THEN 40 ELSE 78 END;
            v_res := CASE WHEN v_ir > 60 THEN 9 WHEN v_grade = 'A' THEN 1 WHEN v_grade = 'B' THEN 2 ELSE 3 END;
            INSERT INTO cell_inspection (cell_id, kind, station_code, attempt, ocv_mv, ir_mohm, result_code,
                                         reason_code, criteria_set_id, started_at, measured_at)
            VALUES (v_cell, 'electrical', '2A-RT', 2, v_ocv, v_ir, v_res,
                    CASE WHEN v_res = 9 THEN 3 ELSE 0 END, v_crit, v_ts + interval '60 s', v_ts + interval '78 s');
            IF v_res = 9 THEN
                UPDATE cell SET status = 'rejected' WHERE cell_id = v_cell;
                CONTINUE;
            END IF;
        END IF;

        UPDATE cell SET status = 'passed', grade = v_grade WHERE cell_id = v_cell;

        -- 박스 적재: 등급별로 채우는 중인 박스에 넣고, 4개 차면 full
        SELECT box_id INTO v_box FROM cell_box
         WHERE grade = v_grade AND status = 'filling' AND run_id = v_run LIMIT 1;
        IF v_box IS NULL THEN
            v_box := format('B%s-%s-%s', lpad(v_run::text, 4, '0'), v_grade,
                            (SELECT count(*) + 1 FROM cell_box WHERE run_id = v_run AND grade = v_grade));
            INSERT INTO cell_box (box_id, grade, run_id, opened_at) VALUES (v_box, v_grade, v_run, v_ts);
        END IF;
        SELECT count(*) + 1 INTO v_slot FROM cell WHERE box_id = v_box;
        UPDATE cell SET status = 'boxed', box_id = v_box, box_slot = v_slot WHERE cell_id = v_cell;
        IF v_slot = 4 THEN
            UPDATE cell_box SET status = 'full', packed_at = v_ts + interval '90 s' WHERE box_id = v_box;
        END IF;
    END LOOP;

    -- ── 확인 ─────────────────────────────────────────────
    IF (SELECT cells_in FROM v_run_yield WHERE run_id = v_run) <> 24 THEN
        RAISE EXCEPTION 'v_run_yield.cells_in';
    END IF;
    IF (SELECT (appearance_ng, retested, retest_recovered, removed)
          FROM v_run_yield WHERE run_id = v_run) IS DISTINCT FROM (1::bigint, 2::bigint, 1::bigint, 2::bigint) THEN
        RAISE EXCEPTION 'v_run_yield counts: %', (SELECT row(v.*) FROM v_run_yield v WHERE run_id = v_run);
    END IF;
    IF (SELECT count(*) FROM v_tester_stats WHERE run_id = v_run AND station_code LIKE '2A-T_') <> 4 THEN
        RAISE EXCEPTION 'v_tester_stats should list 4 tester lanes';
    END IF;
    IF EXISTS (SELECT 1 FROM v_box_spread WHERE run_id = v_run AND cells = 4 AND spread_mv IS NULL) THEN
        RAISE EXCEPTION 'v_box_spread spread missing';
    END IF;
    IF (SELECT reduction_pct FROM v_set_spread_compare WHERE run_id = v_run) IS NULL THEN
        RAISE EXCEPTION 'v_set_spread_compare reduction_pct is null';
    END IF;

    -- 같은 event_id 재전송은 거부 (수집기는 ON CONFLICT DO NOTHING 으로 씀)
    BEGIN
        INSERT INTO event_log (event_id, source, station_code, event_type, entity_id, run_id, occurred_at)
        VALUES (md5(v_run || ':' || 1)::uuid, '2A', '2A-VIS1', 'cell.id_read', 'dup', v_run, now());
        RAISE EXCEPTION 'duplicate event_id was accepted';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;

    -- 같은 셀·같은 시도 번호 검사 중복 거부
    BEGIN
        INSERT INTO cell_inspection (cell_id, kind, station_code, result_code, measured_at)
        VALUES (format('R%s-%s', lpad(v_run::text, 4, '0'), '000001'), 'id_read', '2A-VIS1', 0, now());
        RAISE EXCEPTION 'duplicate inspection attempt was accepted';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;

    -- 전압 검사는 기준값 없이 못 씀
    BEGIN
        INSERT INTO cell_inspection (cell_id, kind, station_code, attempt, result_code, measured_at)
        VALUES (format('R%s-%s', lpad(v_run::text, 4, '0'), '000001'), 'electrical', '2A-T1', 9, 1, now());
        RAISE EXCEPTION 'electrical without criteria_set was accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    -- LOT 보류 트리거: 보류 → is_held true, 해제 → false
    INSERT INTO lot_hold (lot_code, ng_count) VALUES ('4', 3);
    IF NOT (SELECT is_held FROM lot WHERE lot_code = '4') THEN RAISE EXCEPTION 'lot hold not synced'; END IF;
    UPDATE lot_hold SET released_at = now(), released_by = 'smoke' WHERE lot_code = '4' AND released_at IS NULL;
    IF (SELECT is_held FROM lot WHERE lot_code = '4') THEN RAISE EXCEPTION 'lot release not synced'; END IF;

    -- 설비 상태: 스테이션마다 열린 상태는 하나
    INSERT INTO equipment_state (station_code, run_id, state, started_at) VALUES ('2A-T1', v_run, 'busy', v_t0);
    BEGIN
        INSERT INTO equipment_state (station_code, run_id, state, started_at) VALUES ('2A-T1', v_run, 'idle', v_t0);
        RAISE EXCEPTION 'two open states accepted';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;

    -- 인계 → 팩 1개
    SELECT box_id INTO v_box FROM cell_box WHERE run_id = v_run AND status = 'full' ORDER BY box_id LIMIT 1;
    INSERT INTO supply_handoff (request_id, box_id, requested_by) VALUES ('REQ-SMOKE-1', v_box, 'PACK');
    UPDATE supply_handoff SET status = 'released', released_count = 4, released_at = now() WHERE request_id = 'REQ-SMOKE-1';
    UPDATE supply_handoff SET status = 'confirmed', confirmed_at = now() WHERE request_id = 'REQ-SMOKE-1';
    UPDATE cell_box SET status = 'handed_over' WHERE box_id = v_box;

    INSERT INTO pack_recipe (pack_type, version) VALUES (4, 1);
    INSERT INTO recipe_slot SELECT recipe_id, s, 1 FROM pack_recipe, generate_series(1, 4) s WHERE pack_type = 4;
    INSERT INTO work_order (job_id, pack_type, recipe_id) SELECT 'JOB-SMOKE', 4, recipe_id FROM pack_recipe WHERE pack_type = 4;
    INSERT INTO pack (pack_id, job_id, pack_type, recipe_id) SELECT 'PK-SMOKE', 'JOB-SMOKE', 4, recipe_id FROM pack_recipe WHERE pack_type = 4;
    INSERT INTO pack_slot (pack_id, slot_no, cell_id, source_box_id, orientation)
    SELECT 'PK-SMOKE', box_slot, cell_id, box_id, 1 FROM cell WHERE box_id = v_box;
    UPDATE cell SET status = 'in_pack' WHERE box_id = v_box;

    IF (SELECT count(*) FROM v_pack_trace WHERE pack_id = 'PK-SMOKE' AND ocv_mv IS NOT NULL) <> 4 THEN
        RAISE EXCEPTION 'v_pack_trace should trace 4 cells to 2A measurements';
    END IF;
    IF (SELECT handed_over FROM v_run_yield WHERE run_id = v_run) <> 4 THEN
        RAISE EXCEPTION 'v_run_yield.handed_over';
    END IF;

    -- 한 셀을 두 팩에 넣을 수 없음
    BEGIN
        INSERT INTO pack (pack_id, job_id, pack_type, recipe_id) SELECT 'PK-SMOKE-2', 'JOB-SMOKE', 4, recipe_id FROM pack_recipe WHERE pack_type = 4;
        INSERT INTO pack_slot (pack_id, slot_no, cell_id) SELECT 'PK-SMOKE-2', 1, cell_id FROM cell WHERE box_id = v_box LIMIT 1;
        RAISE EXCEPTION 'cell placed in two packs';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;

    RAISE NOTICE 'smoke OK (run %)', v_run;
END $$;

SELECT * FROM v_run_yield;
SELECT * FROM v_set_spread_compare;
SELECT box_id, grade, cells, spread_mv FROM v_box_spread ORDER BY box_id;
SELECT * FROM v_tester_stats ORDER BY station_code;

ROLLBACK;
