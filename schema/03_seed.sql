-- 기준 데이터: 스테이션 목록, 초기 판정 기준값. 02_views.sql 다음에 실행.
-- 다시 실행해도 안전 (ON CONFLICT DO NOTHING).

BEGIN;

-- 2A 공정 v2 (11/23 Factory I/O 배치 기준). 코드는 digital-twin-a docs/2a-data-design.md 수집점과 같음
INSERT INTO station (station_code, line, name, kind, lane, sort_order) VALUES
    ('2A-IN',   '2A', '투입',                'infeed',   NULL, 10),
    ('2A-VIS1', '2A', '1차 비전 (ID)',       'vision',   NULL, 20),
    ('2A-VIS2', '2A', '2차 비전 (외관)',     'vision',   NULL, 30),
    ('2A-RJ1',  '2A', '외관 NG 배출',        'remover',  NULL, 39),
    ('2A-T1',   '2A', '전압·저항 검사기 1',  'tester',   1,    41),
    ('2A-T2',   '2A', '전압·저항 검사기 2',  'tester',   2,    42),
    ('2A-T3',   '2A', '전압·저항 검사기 3',  'tester',   3,    43),
    ('2A-T4',   '2A', '전압·저항 검사기 4',  'tester',   4,    44),
    ('2A-IDC',  '2A', '분류 전 ID 재확인',   'vision',   NULL, 55),
    ('2A-SORT', '2A', 'A/B/C 분류',          'sorter',   NULL, 56),
    ('2A-LA',   '2A', 'A 레인',              'lane',     1,    61),
    ('2A-LB',   '2A', 'B 레인',              'lane',     2,    62),
    ('2A-LC',   '2A', 'C 레인',              'lane',     3,    63),
    ('2A-RT',   '2A', '재검 스테이션',       'retest',   NULL, 70),
    ('2A-RJ2',  '2A', '최종 NG 배출',        'remover',  NULL, 79),
    ('2A-BOX',  '2A', '박스 적재',           'packer',   NULL, 80),
    ('2A-OUT',  '2A', '박스 배출 (팩 라인)', 'handoff',  NULL, 90)
ON CONFLICT (station_code) DO NOTHING;

-- 팩 라인 (docs/schema.md 목록 그대로. 이름·종류는 팩 팀이 고칠 것)
INSERT INTO station (station_code, line, name, kind, lane, sort_order) VALUES
    ('PACK-VIS1', 'PACK', '팔레트 비전',   'vision',   NULL, 110),
    ('PACK-R1',   'PACK', '셀 삽입 로봇',  'robot',    NULL, 120),
    ('PACK-VIS2', 'PACK', '조립 비전',     'vision',   NULL, 130),
    ('PACK-R2',   'PACK', '로봇 2',        'robot',    NULL, 140),
    ('PACK-VT',   'PACK', '팩 전압 검사',  'tester',   NULL, 150),
    ('PACK-R3',   'PACK', '분류 로봇',     'robot',    NULL, 160)
ON CONFLICT (station_code) DO NOTHING;

-- 초기 판정 기준 (interface.md D2200~ 초기값). 이미 있으면 넣지 않음
INSERT INTO criteria_set (ocv_min_mv, ocv_max_mv, ir_max_mohm, grade_a_min_mv, grade_b_min_mv, lot_ng_limit, retest_limit)
SELECT 3500, 3800, 60, 3750, 3650, 3, 1
WHERE NOT EXISTS (SELECT 1 FROM criteria_set);

COMMIT;
