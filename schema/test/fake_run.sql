-- 가짜 시험 1회 (sim_run.purpose = 'fake'). Factory I/O·수집기 전에 SCADA(Grafana)·API 화면을 만들기 위한 데이터.
-- 실행할 때마다 새 run이 하나 생긴다. 지우기: fake_cleanup.sql (purpose='fake'만 지움)
--   sudo -u postgres psql -d battery -c 'SET ROLE battery' -f schema/test/fake_run.sql
--
-- 들어가는 것 (약 45분짜리 운전, 셀 100개)
--   LOT 3개 (F<run>-1~3), 투입 24초 간격
--   ID 판독 → 외관(약 4% NG) → 4레인 전압 검사 → NG면 재검 1회 → 등급별 4개 박스
--   일부러 넣은 편향: 2A-T2 내부저항이 높게 나옴(NG 많음), 2A-T3 검사 시간이 김
--   설비 상태 전이(2A-T2 고장 3분, 2A-BOX 막힘 2분), 알람(해제된 것 + 열린 경고 1개),
--   확정 NG 3건이 된 LOT는 보류(lot_hold), 다 찬 박스 일부는 팩 라인 인계
--   event_log: 검사 1건 = 이벤트 1건 (api_client = 'fake-generator')

BEGIN;

SELECT setseed(0.4242);

DO $$
DECLARE
    v_run   integer;
    v_crit  integer;
    v_t0    timestamptz := date_trunc('minute', now()) - interval '60 minutes';
    v_end   timestamptz;
    n       integer;
    v_cell  text;
    v_lot   text;
    v_ts    timestamptz;
    v_lane  integer;
    v_st    text;
    v_start timestamptz;
    v_meas  timestamptz;
    v_ocv   integer;
    v_ir    integer;
    v_res   integer;
    v_reason integer;
    v_att   integer;
    v_grade char(1);
    v_box   text;
    v_slot  integer;
    v_ng    integer;
    r       record;
    t       timestamptz;
    d       interval;
    s       text;
BEGIN
    v_end := v_t0 + interval '45 minutes';
    INSERT INTO sim_run (line, purpose, scene_version, ladder_version, started_at, ended_at, note, api_client)
    VALUES ('2A', 'fake', 'fake', 'fake', v_t0, v_end,
            '가짜 데이터 (SCADA 화면 개발용). schema/test/fake_cleanup.sql 로 삭제', 'fake-generator')
    RETURNING run_id INTO v_run;

    SELECT id INTO v_crit FROM criteria_set ORDER BY valid_from DESC, id DESC LIMIT 1;
    FOR n IN 1..3 LOOP
        INSERT INTO lot (lot_code, received_at) VALUES (format('F%s-%s', v_run, n), v_t0);
    END LOOP;

    FOR n IN 1..100 LOOP
        v_ts   := v_t0 + n * interval '24 seconds';
        v_lot  := format('F%s-%s', v_run, 1 + (n - 1) / 34);
        v_cell := format('R%s-%s', lpad(v_run::text, 4, '0'), lpad(n::text, 6, '0'));
        INSERT INTO cell (cell_id, lot_code, run_id, plc_no, created_at) VALUES (v_cell, v_lot, v_run, n, v_ts);

        INSERT INTO cell_inspection (cell_id, kind, station_code, result_code, read_status, measured_at)
        VALUES (v_cell, 'id_read', '2A-VIS1', 0, 0, v_ts + interval '3 s');

        -- 외관
        IF random() < 0.04 THEN
            INSERT INTO cell_inspection (cell_id, kind, station_code, result_code, reason_code, measured_at)
            VALUES (v_cell, 'appearance', '2A-VIS2', 9, 11 + floor(random() * 3)::int, v_ts + interval '6 s');
            UPDATE cell SET status = 'rejected' WHERE cell_id = v_cell;
            CONTINUE;
        END IF;
        INSERT INTO cell_inspection (cell_id, kind, station_code, result_code, measured_at)
        VALUES (v_cell, 'appearance', '2A-VIS2', 0, v_ts + interval '6 s');

        -- 전압 검사 (1차: 4레인, 재검: 2A-RT)
        v_lane := 1 + (n % 4);
        v_ocv  := CASE WHEN random() < 0.04 THEN round(3460 + random() * 35)          -- 과방전 셀
                       ELSE round(3545 + random() * 245 + (random() - 0.5) * 30) END;
        v_ir   := round(32 + random() * 20 + CASE WHEN v_lane = 2 THEN random() * 22 ELSE 0 END);
        v_start := v_ts + interval '12 s';
        FOR v_att IN 1..2 LOOP
            v_st := CASE WHEN v_att = 1 THEN '2A-T' || v_lane ELSE '2A-RT' END;
            v_meas := v_start + make_interval(secs => CASE WHEN v_st = '2A-T3' THEN 24 ELSE 18 END + random() * 4);
            v_reason := CASE WHEN v_ocv < 3500 THEN 1 WHEN v_ocv > 3800 THEN 2 WHEN v_ir > 60 THEN 3 ELSE 0 END;
            v_res := CASE WHEN v_reason > 0 THEN 9 WHEN v_ocv >= 3750 THEN 1 WHEN v_ocv >= 3650 THEN 2 ELSE 3 END;
            INSERT INTO cell_inspection (cell_id, kind, station_code, attempt, ocv_mv, v1_mv, i1_ma, v2_mv, i2_ma,
                                         ir_mohm, orientation, read_status, result_code, reason_code,
                                         criteria_set_id, started_at, measured_at)
            VALUES (v_cell, 'electrical', v_st, v_att, v_ocv, v_ocv - v_ir, 1000, v_ocv - v_ir * 3, 3000,
                    v_ir, 1, 0, v_res, v_reason, v_crit, v_start, v_meas);
            EXIT WHEN v_res <> 9;
            UPDATE cell SET status = 'retest' WHERE cell_id = v_cell;
            -- 재검: 절반은 접촉 불량이라 회복, 나머지는 그대로 NG
            v_ir := CASE WHEN random() < 0.5 THEN v_ir - 12 - round(random() * 8) ELSE v_ir + round(random() * 3) END;
            v_start := v_meas + interval '40 s';
        END LOOP;

        IF v_res = 9 THEN
            UPDATE cell SET status = 'rejected' WHERE cell_id = v_cell;
            SELECT count(*) INTO v_ng FROM cell c JOIN cell_inspection i USING (cell_id)
             WHERE c.lot_code = v_lot AND i.kind = 'electrical' AND i.result_code = 9
               AND i.attempt = (SELECT max(attempt) FROM cell_inspection j WHERE j.cell_id = c.cell_id AND j.kind = 'electrical');
            IF v_ng >= 3 AND NOT EXISTS (SELECT 1 FROM lot_hold WHERE lot_code = v_lot) THEN
                INSERT INTO lot_hold (lot_code, held_at, ng_count, released_at, released_by, note)
                VALUES (v_lot, v_meas, v_ng, v_meas + interval '5 minutes', 'fake-operator', '가짜: 확정 NG 3건');
            END IF;
            CONTINUE;
        END IF;

        -- 등급별 박스 적재
        v_grade := CASE v_res WHEN 1 THEN 'A' WHEN 2 THEN 'B' ELSE 'C' END;
        SELECT box_id INTO v_box FROM cell_box WHERE run_id = v_run AND grade = v_grade AND status = 'filling';
        IF v_box IS NULL THEN
            v_box := format('B%s-%s%s', lpad(v_run::text, 4, '0'), v_grade,
                            lpad((1 + (SELECT count(*) FROM cell_box WHERE run_id = v_run AND grade = v_grade))::text, 3, '0'));
            INSERT INTO cell_box (box_id, grade, run_id, opened_at) VALUES (v_box, v_grade, v_run, v_meas + interval '20 s');
        END IF;
        SELECT count(*) + 1 INTO v_slot FROM cell WHERE box_id = v_box;
        UPDATE cell SET status = 'boxed', grade = v_grade, box_id = v_box, box_slot = v_slot WHERE cell_id = v_cell;
        IF v_slot = 4 THEN
            UPDATE cell_box SET status = 'full', packed_at = v_meas + interval '25 s' WHERE box_id = v_box;
        END IF;
    END LOOP;

    -- 다 찬 박스 중 앞의 3개는 인계 확인, 4번째는 배출만
    n := 0;
    FOR r IN SELECT box_id, packed_at FROM cell_box WHERE run_id = v_run AND status = 'full' ORDER BY packed_at LOOP
        n := n + 1;
        EXIT WHEN n > 4;
        INSERT INTO supply_handoff (request_id, box_id, requested_by, requested_at, released_count, released_at, confirmed_at, status)
        VALUES (format('FAKE-%s-%s', v_run, n), r.box_id, 'PACK', r.packed_at + interval '1 minute', 4,
                r.packed_at + interval '90 s', CASE WHEN n <= 3 THEN r.packed_at + interval '2 minutes' END,
                CASE WHEN n <= 3 THEN 'confirmed' ELSE 'released' END);
        IF n <= 3 THEN
            UPDATE cell_box SET status = 'handed_over' WHERE box_id = r.box_id;
        END IF;
    END LOOP;

    -- 원본 이벤트 (검사 1건 = 1건)
    INSERT INTO event_log (event_id, source, station_code, event_type, entity_id, run_id, payload, occurred_at, api_client)
    SELECT gen_random_uuid(), '2A', i.station_code, 'cell.' || i.kind, i.cell_id, v_run,
           jsonb_strip_nulls(jsonb_build_object('attempt', i.attempt, 'result_code', i.result_code,
                                                'reason_code', i.reason_code, 'ocv_mv', i.ocv_mv, 'ir_mohm', i.ir_mohm)),
           i.measured_at, 'fake-generator'
    FROM cell_inspection i JOIN cell c USING (cell_id)
    WHERE c.run_id = v_run;

    -- 설비 상태 전이 (전부 닫힌 구간. 열린 상태는 남기지 않음)
    FOR s IN SELECT station_code FROM station WHERE line = '2A' ORDER BY sort_order LOOP
        t := v_t0;
        WHILE t < v_end LOOP
            d := make_interval(secs => 60 + random() * 180);
            INSERT INTO equipment_state (station_code, run_id, state, started_at, ended_at)
            VALUES (s, v_run,
                    CASE WHEN s = '2A-T2' AND t >= v_t0 + interval '20 min' AND t < v_t0 + interval '23 min' THEN 'fault'
                         WHEN s = '2A-BOX' AND t >= v_t0 + interval '30 min' AND t < v_t0 + interval '33 min' THEN 'blocked'
                         WHEN s = '2A-IN' AND t >= v_t0 + interval '41 min' THEN 'starved'
                         WHEN random() < 0.75 THEN 'busy' ELSE 'idle' END,
                    t, least(t + d, v_end));
            t := t + d;
        END LOOP;
    END LOOP;

    -- 알람
    INSERT INTO alarm (station_code, run_id, code, severity, message, raised_at, acked_at, acked_by, cleared_at) VALUES
      ('2A-T2',  v_run, 'JAM',          3, '[fake] 검사기 2 지그 걸림',   v_t0 + interval '20 min', v_t0 + interval '21 min', 'fake-operator', v_t0 + interval '23 min'),
      ('2A-BOX', v_run, 'BOX_MISSING',  2, '[fake] 빈 박스 공급 지연',    v_t0 + interval '30 min', v_t0 + interval '31 min', 'fake-operator', v_t0 + interval '32 min'),
      ('2A-IDC', v_run, 'ID_MISMATCH',  2, '[fake] 합류 후 ID 순서 불일치', v_t0 + interval '38 min', NULL, NULL, NULL);

    RAISE NOTICE 'fake run % created', v_run;
END $$;

-- 확인
SELECT run_id, cells_in, appearance_ng, first_pass_ok, retested, retest_recovered, removed, boxed, handed_over
FROM v_run_yield WHERE run_id = (SELECT max(run_id) FROM sim_run WHERE purpose = 'fake');
SELECT station_code, tests, ng_pct, avg_test_s FROM v_tester_stats
WHERE run_id = (SELECT max(run_id) FROM sim_run WHERE purpose = 'fake') ORDER BY station_code;
SELECT * FROM v_set_spread_compare WHERE run_id = (SELECT max(run_id) FROM sim_run WHERE purpose = 'fake');

COMMIT;
