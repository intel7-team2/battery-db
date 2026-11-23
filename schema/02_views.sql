-- 대시보드·SCADA 조회용 뷰. 01_tables.sql 다음에 실행.
-- 뷰는 읽기 전용이라 다시 만들어도 데이터에 영향 없음 (CREATE OR REPLACE).

BEGIN;

-- 셀마다 가장 최근 전압 검사 (재검했으면 재검 결과)
CREATE OR REPLACE VIEW v_cell_electrical_latest AS
SELECT DISTINCT ON (ci.cell_id)
       ci.cell_id, ci.attempt, ci.station_code, ci.ocv_mv, ci.ir_mohm,
       ci.result_code, ci.reason_code, ci.criteria_set_id, ci.measured_at
FROM cell_inspection ci
WHERE ci.kind = 'electrical'
ORDER BY ci.cell_id, ci.attempt DESC;

-- 박스별 세트 편차 (최대 - 최소 OCV)
CREATE OR REPLACE VIEW v_box_spread AS
SELECT b.box_id, b.grade, b.status, b.run_id, b.packed_at,
       count(c.cell_id)                AS cells,
       min(e.ocv_mv)                   AS ocv_min_mv,
       max(e.ocv_mv)                   AS ocv_max_mv,
       max(e.ocv_mv) - min(e.ocv_mv)   AS spread_mv
FROM cell_box b
LEFT JOIN cell c                     ON c.box_id = b.box_id
LEFT JOIN v_cell_electrical_latest e ON e.cell_id = c.cell_id
GROUP BY b.box_id;

-- 계획서 핵심 지표: 같은 합격 셀로 "투입 순서대로 4개씩" 묶었을 때 vs 실제 등급별 박스
-- 감소율(%) = (순서별 평균 편차 - 구간별 평균 편차) / 순서별 평균 편차 * 100
CREATE OR REPLACE VIEW v_set_spread_compare AS
WITH passed AS (
    SELECT c.run_id, c.cell_id, e.ocv_mv,
           (row_number() OVER (PARTITION BY c.run_id ORDER BY c.plc_no) - 1) / 4 AS seq_set
    FROM cell c
    JOIN v_cell_electrical_latest e ON e.cell_id = c.cell_id
    WHERE c.box_id IN (SELECT box_id FROM v_box_spread WHERE cells = 4)  -- 다 찬 박스의 셀만 (두 방식이 같은 셀을 씀)
),
seq_sets AS (
    SELECT run_id, seq_set, max(ocv_mv) - min(ocv_mv) AS spread_mv
    FROM passed GROUP BY run_id, seq_set HAVING count(*) = 4
),
box_sets AS (
    SELECT run_id, spread_mv FROM v_box_spread WHERE cells = 4
)
SELECT r.run_id,
       (SELECT count(*) FROM box_sets b WHERE b.run_id = r.run_id)                 AS sets,
       (SELECT round(avg(spread_mv), 1) FROM seq_sets s WHERE s.run_id = r.run_id) AS seq_avg_spread_mv,
       (SELECT round(avg(spread_mv), 1) FROM box_sets b WHERE b.run_id = r.run_id) AS grade_avg_spread_mv,
       round(100 * (1 - (SELECT avg(spread_mv) FROM box_sets b WHERE b.run_id = r.run_id)
                      / nullif((SELECT avg(spread_mv) FROM seq_sets s WHERE s.run_id = r.run_id), 0)), 1)
                                                                                    AS reduction_pct
FROM sim_run r;

-- 런별 수율: 투입 → 외관 → 1차 검사 → 재검 → 박스 → 인계
CREATE OR REPLACE VIEW v_run_yield AS
SELECT c.run_id,
       count(*)                                                                         AS cells_in,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM cell_inspection i WHERE i.cell_id = c.cell_id
                                       AND i.kind = 'appearance' AND i.result_code = 9)) AS appearance_ng,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM cell_inspection i WHERE i.cell_id = c.cell_id
                                       AND i.kind = 'electrical' AND i.attempt = 1
                                       AND i.result_code IN (1, 2, 3)))                 AS first_pass_ok,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM cell_inspection i WHERE i.cell_id = c.cell_id
                                       AND i.kind = 'electrical' AND i.attempt > 1))    AS retested,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM cell_inspection i WHERE i.cell_id = c.cell_id
                                       AND i.kind = 'electrical' AND i.attempt > 1
                                       AND i.result_code IN (1, 2, 3)))                 AS retest_recovered,
       count(*) FILTER (WHERE c.status IN ('rejected', 'scrapped'))                     AS removed,
       count(*) FILTER (WHERE c.box_id IS NOT NULL)                                     AS boxed,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM supply_handoff h WHERE h.box_id = c.box_id
                                       AND h.status = 'confirmed'))                     AS handed_over
FROM cell c
WHERE c.run_id IS NOT NULL
GROUP BY c.run_id;

-- 검사기 레인별 1차 검사 NG율·평균 검사 시간 (장비 편향 확인)
CREATE OR REPLACE VIEW v_tester_stats AS
SELECT i.station_code, s.lane, c.run_id,
       count(*)                                                        AS tests,
       round(avg((i.result_code = 9)::int) * 100, 1)                   AS ng_pct,
       round(avg(extract(epoch FROM i.measured_at - i.started_at))::numeric, 1) AS avg_test_s
FROM cell_inspection i
JOIN station s ON s.station_code = i.station_code
JOIN cell c    ON c.cell_id = i.cell_id
WHERE i.kind = 'electrical'
GROUP BY i.station_code, s.lane, c.run_id;

-- 설비 상태별 누적 시간 (가동률·병목). 열린 상태는 지금까지로 계산
CREATE OR REPLACE VIEW v_equipment_time AS
SELECT station_code, run_id, state,
       round(sum(extract(epoch FROM coalesce(ended_at, now()) - started_at))::numeric, 1) AS seconds
FROM equipment_state
GROUP BY station_code, run_id, state;

-- LOT 현황: 보류 여부, 확정 NG 누계 (최종 전압 검사 NG만. 대기·미판독은 안 셈)
CREATE OR REPLACE VIEW v_lot_status AS
SELECT l.lot_code, l.is_held,
       count(c.cell_id)                                   AS cells,
       count(*) FILTER (WHERE e.result_code = 9)          AS confirmed_ng,
       (SELECT max(held_at) FROM lot_hold h WHERE h.lot_code = l.lot_code) AS last_held_at
FROM lot l
LEFT JOIN cell c                     ON c.lot_code = l.lot_code
LEFT JOIN v_cell_electrical_latest e ON e.cell_id = c.cell_id
GROUP BY l.lot_code;

-- 팩 한 개 이력: 팩 → 슬롯 → 셀 → LOT·박스·2A 검사값
CREATE OR REPLACE VIEW v_pack_trace AS
SELECT p.pack_id, p.status AS pack_status, ps.slot_no, ps.cell_id, c.lot_code,
       c.grade, ps.source_box_id, e.ocv_mv, e.ir_mohm, e.station_code AS tester, e.attempt
FROM pack p
JOIN pack_slot ps                    ON ps.pack_id = p.pack_id
JOIN cell c                          ON c.cell_id = ps.cell_id
LEFT JOIN v_cell_electrical_latest e ON e.cell_id = c.cell_id;

-- 열린 알람
CREATE OR REPLACE VIEW v_alarm_open AS
SELECT * FROM alarm WHERE cleared_at IS NULL ORDER BY severity DESC, raised_at;

COMMIT;
