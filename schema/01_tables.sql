-- battery-db 테이블 (PostgreSQL 16)
-- 근거: docs/schema.md (11/20 초안) + 2A 공정 v2 (11/23, 4레인 검사기·재검·2단 비전) 반영분
-- 2A v2로 새로 넣거나 바꾼 곳은 "-- [v2]" 표시.
--
-- 규칙 (docs/README.md)
--   * 테이블마다 쓰는 쪽은 하나. 다른 쪽은 읽기만 (04_roles.sql).
--   * 이력은 지우지 않는다. 상태가 바뀌면 검사·이벤트 행을 추가한다.
--   * DB에는 이벤트만. PLC 주기값(센서, 하트비트)은 쌓지 않는다.
--
-- 다시 만들 때: psql -h localhost -p 5433 -U battery -d battery -f 01_tables.sql
-- (빈 DB 기준. 이미 있으면 에러로 멈춘다. 지우고 다시 만드는 스크립트는 일부러 두지 않음)

BEGIN;

-- ════════════════════════════════════════════════════════════════
-- 1. 공통
-- ════════════════════════════════════════════════════════════════

CREATE TABLE station (
    station_code text PRIMARY KEY,                 -- '2A-VIS1', '2A-T3', 'PACK-R1'
    line         text NOT NULL CHECK (line IN ('2A', 'PACK', '2B')),
    name         text NOT NULL,                    -- 화면 표시 이름
    kind         text NOT NULL,                    -- [v2] infeed, vision, tester, retest, sorter, lane, packer, remover, handoff, robot, assembly
    lane         smallint,                         -- [v2] 병렬 설비의 레인 번호 (검사기 1~4)
    sort_order   smallint NOT NULL DEFAULT 0       -- [v2] 화면·리포트 정렬
);

-- [v2] 시연·시험 1회. Factory I/O는 재시작마다 PLC 셀 번호가 1부터라 run으로 구분한다.
--      실물 데이터(팩 라인 등)는 run 없이 써도 된다.
CREATE TABLE sim_run (
    run_id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    line           text NOT NULL CHECK (line IN ('2A', 'PACK', '2B')),
    purpose        text NOT NULL DEFAULT 'demo',   -- demo, test, fault-test ...
    scene_version  text,                           -- 씬·래더 커밋 해시
    ladder_version text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    ended_at       timestamptz,
    note           text
);

-- 모든 쓰기의 원본 기록. 수집기가 같은 이벤트를 다시 보내도 event_id가 같아 한 번만 들어간다.
-- 2A 수집기는 event_id = uuid v5(namespace, 'run:<run_id>/seq:<PLC 메일박스 순번>') 로 만든다.
CREATE TABLE event_log (
    event_id     uuid PRIMARY KEY,
    source       text NOT NULL CHECK (source IN ('2A', 'PACK', '2B')),
    station_code text NOT NULL REFERENCES station,
    event_type   text NOT NULL,                    -- 'cell.inspected', 'pack.slot_filled' ...
    entity_id    text,                             -- 대상 셀·팩·박스 ID
    run_id       integer REFERENCES sim_run,       -- [v2]
    payload      jsonb NOT NULL DEFAULT '{}',      -- 원본 값 (PLC 블록 그대로)
    occurred_at  timestamptz NOT NULL,             -- 현장 시각
    received_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX event_log_entity_idx  ON event_log (entity_id, occurred_at);
CREATE INDEX event_log_station_idx ON event_log (station_code, occurred_at);

-- [v2] 설비 상태 전이. 주기 샘플이 아니라 상태가 바뀔 때 1행 (끝나면 ended_at 채움).
CREATE TABLE equipment_state (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    station_code text NOT NULL REFERENCES station,
    run_id       integer REFERENCES sim_run,
    state        text NOT NULL CHECK (state IN ('idle', 'busy', 'blocked', 'starved', 'fault', 'stopped')),
    started_at   timestamptz NOT NULL,
    ended_at     timestamptz,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE INDEX equipment_state_station_idx ON equipment_state (station_code, started_at);
-- 스테이션마다 열린(끝나지 않은) 상태는 하나만
CREATE UNIQUE INDEX equipment_state_open_uq ON equipment_state (station_code) WHERE ended_at IS NULL;

-- [v2] 알람: 발생·확인·해제
CREATE TABLE alarm (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    station_code text REFERENCES station,
    run_id       integer REFERENCES sim_run,
    code         text NOT NULL,                    -- 'JAM', 'SENSOR_STUCK', 'COMM_LOST', 'ID_MISMATCH' ...
    severity     smallint NOT NULL CHECK (severity BETWEEN 1 AND 3),  -- 1 정보, 2 경고, 3 정지
    message      text,
    raised_at    timestamptz NOT NULL,
    acked_at     timestamptz,
    acked_by     text,
    cleared_at   timestamptz
);
CREATE INDEX alarm_open_idx ON alarm (raised_at) WHERE cleared_at IS NULL;

-- ════════════════════════════════════════════════════════════════
-- 2. 셀 (2A가 씀)
-- ════════════════════════════════════════════════════════════════

CREATE TABLE lot (
    lot_code    text PRIMARY KEY,                  -- interface.md D2102 (지금은 '1' / '4' / '7')
    received_at timestamptz NOT NULL DEFAULT now(),
    is_held     boolean NOT NULL DEFAULT false     -- lot_hold 트리거가 맞춰 줌. 직접 고치지 않는다
);

CREATE TABLE lot_hold (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lot_code    text NOT NULL REFERENCES lot,
    held_at     timestamptz NOT NULL DEFAULT now(),
    ng_count    integer NOT NULL,                  -- 보류 시점의 확정 NG 누계 (기준 3건)
    released_at timestamptz,                       -- null이면 보류 중
    released_by text,
    note        text,
    CHECK (released_at IS NULL OR released_by IS NOT NULL)
);
-- LOT마다 열린 보류는 하나만
CREATE UNIQUE INDEX lot_hold_open_uq ON lot_hold (lot_code) WHERE released_at IS NULL;

CREATE FUNCTION lot_hold_sync() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE lot l
       SET is_held = EXISTS (SELECT 1 FROM lot_hold h
                              WHERE h.lot_code = l.lot_code AND h.released_at IS NULL)
     WHERE l.lot_code = NEW.lot_code;
    RETURN NULL;
END $$;
CREATE TRIGGER lot_hold_sync AFTER INSERT OR UPDATE ON lot_hold
    FOR EACH ROW EXECUTE FUNCTION lot_hold_sync();

-- 판정 기준값 (D2200~). SCADA에서 바꿀 때마다 새 행. 최신 valid_from이 현재 기준.
CREATE TABLE criteria_set (
    id             integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ocv_min_mv     integer NOT NULL,               -- D2200
    ocv_max_mv     integer NOT NULL,               -- D2201
    ir_max_mohm    integer NOT NULL,               -- D2202
    grade_a_min_mv integer NOT NULL,               -- D2203 (A/B 경계)
    grade_b_min_mv integer NOT NULL,               -- D2204 (B/C 경계)
    lot_ng_limit   integer NOT NULL,               -- D2205
    retest_limit   smallint NOT NULL DEFAULT 1,    -- [v2] 재검 허용 횟수
    valid_from     timestamptz NOT NULL DEFAULT now(),
    changed_by     text NOT NULL DEFAULT 'init',   -- [v2] 누가 (HMI 사용자)
    source         text NOT NULL DEFAULT 'init',   -- [v2] init, hmi, script
    CHECK (ocv_min_mv < grade_b_min_mv AND grade_b_min_mv < grade_a_min_mv AND grade_a_min_mv <= ocv_max_mv)
);

CREATE TABLE cell_box (
    box_id    text PRIMARY KEY,                    -- 박스 QR (가상은 수집기가 부여)
    grade     char(1) NOT NULL CHECK (grade IN ('A', 'B', 'C')),
    capacity  smallint NOT NULL DEFAULT 4,
    status    text NOT NULL DEFAULT 'filling'
              CHECK (status IN ('filling', 'full', 'handed_over', 'consumed')),
    run_id    integer REFERENCES sim_run,          -- [v2]
    opened_at timestamptz NOT NULL DEFAULT now(),  -- [v2] 첫 셀 적재 시각
    packed_at timestamptz                          -- 4셀 다 찬 시각
);

CREATE TABLE cell (
    cell_id    text PRIMARY KEY,                   -- 가상: 'R<run 4자리>-<PLC 번호 6자리>' 예 'R0012-000037'
    lot_code   text REFERENCES lot,                -- 미판독이면 null
    run_id     integer REFERENCES sim_run,         -- [v2] 가상 셀이면 필수, 실물이면 null
    plc_no     integer,                            -- [v2] D2100 32비트 일련번호 (run 안에서만 유일)
    status     text NOT NULL DEFAULT 'registered'
               CHECK (status IN ('registered', 'waiting', 'retest', 'passed', 'rejected',
                                 'boxed', 'in_pack', 'scrapped')),  -- [v2] retest 추가
    grade      char(1) CHECK (grade IN ('A', 'B', 'C')),
    box_id     text REFERENCES cell_box,
    box_slot   smallint CHECK (box_slot BETWEEN 1 AND 4),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, plc_no),
    UNIQUE (box_id, box_slot),
    CHECK ((box_id IS NULL) = (box_slot IS NULL))
);
CREATE INDEX cell_lot_idx ON cell (lot_code);
CREATE INDEX cell_box_idx ON cell (box_id);

-- 검사 1회에 1행. 재측정·재검이면 행 추가 (attempt +1).
CREATE TABLE cell_inspection (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cell_id         text NOT NULL REFERENCES cell,
    kind            text NOT NULL CHECK (kind IN ('id_read', 'appearance', 'electrical')),  -- [v2] id_read 추가
    station_code    text NOT NULL REFERENCES station,  -- 전압 검사면 2A-T1~T4 또는 2A-RT(재검)
    attempt         smallint NOT NULL DEFAULT 1,       -- [v2] 1 = 1차, 2 = 재검
    ocv_mv          integer,                           -- D2103
    v1_mv           integer,                           -- D2104
    i1_ma           integer,                           -- D2105
    v2_mv           integer,                           -- D2106
    i2_ma           integer,                           -- D2107
    ir_mohm         integer,                           -- D2304. 못 재면 null
    orientation     smallint,                          -- D2108
    read_status     smallint,                          -- D2109 (0 정상, 1 QR 미판독, 2 측정 실패)
    result_code     smallint NOT NULL,                 -- 전압: D2302 (1 A, 2 B, 3 C, 5 대기, 9 NG)
                                                       -- 외관·ID: 0 OK, 5 대기(미판독), 9 NG
    reason_code     smallint NOT NULL DEFAULT 0,       -- D2303 (0 없음 … 6 LOT 보류). 외관 NG면 결함 코드
    criteria_set_id integer REFERENCES criteria_set,   -- 전압 검사에서 필수
    seq_no          integer,                           -- D2110 전송 번호
    started_at      timestamptz,                       -- [v2] 검사기 투입 시각 (검사 시간 = measured_at - started_at)
    measured_at     timestamptz NOT NULL,
    event_id        uuid REFERENCES event_log,         -- [v2] 이 행을 만든 이벤트
    CHECK (result_code IN (0, 1, 2, 3, 5, 9)),
    CHECK (kind <> 'electrical' OR (criteria_set_id IS NOT NULL AND result_code <> 0)),
    CHECK (kind = 'electrical' OR result_code IN (0, 5, 9)),
    UNIQUE (cell_id, kind, attempt)
);
CREATE INDEX cell_inspection_station_idx ON cell_inspection (station_code, measured_at);

-- 2A → 팩 라인 인계. 같은 request_id로는 다시 배출하지 않는다.
CREATE TABLE supply_handoff (
    request_id     text PRIMARY KEY,
    box_id         text NOT NULL REFERENCES cell_box,
    requested_by   text NOT NULL CHECK (requested_by IN ('PACK', '2B')),
    requested_at   timestamptz NOT NULL DEFAULT now(),
    released_count smallint,
    released_at    timestamptz,                    -- [v2] 박스가 출구 센서를 지난 시각
    confirmed_at   timestamptz,                    -- 수신 확인. null이면 미확인
    status         text NOT NULL DEFAULT 'requested'
                   CHECK (status IN ('requested', 'released', 'confirmed', 'mismatch'))
);
CREATE INDEX supply_handoff_box_idx ON supply_handoff (box_id);

-- ════════════════════════════════════════════════════════════════
-- 3. 팩 (pack-production이 씀) — docs/schema.md 그대로 옮김. 팩 팀 확인 필요
-- ════════════════════════════════════════════════════════════════

CREATE TABLE customer_order (
    order_id   text PRIMARY KEY,
    channel    text NOT NULL CHECK (channel IN ('web', 'manual')),
    pack_type  smallint NOT NULL CHECK (pack_type IN (4, 6)),
    quantity   integer NOT NULL CHECK (quantity > 0),
    status     text NOT NULL DEFAULT 'received'
               CHECK (status IN ('received', 'in_production', 'done', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE pack_recipe (
    recipe_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pack_type smallint NOT NULL CHECK (pack_type IN (4, 6)),
    version   integer NOT NULL,
    note      text,
    UNIQUE (pack_type, version)
);

CREATE TABLE recipe_slot (
    recipe_id   integer NOT NULL REFERENCES pack_recipe,
    slot_no     smallint NOT NULL CHECK (slot_no BETWEEN 1 AND 6),
    orientation smallint NOT NULL CHECK (orientation IN (1, 2)),
    PRIMARY KEY (recipe_id, slot_no)
);

CREATE TABLE work_order (
    job_id     text PRIMARY KEY,
    order_id   text REFERENCES customer_order,     -- 시험 작업이면 null
    pack_type  smallint NOT NULL CHECK (pack_type IN (4, 6)),
    recipe_id  integer NOT NULL REFERENCES pack_recipe,
    status     text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE pallet (
    pallet_id text PRIMARY KEY,
    pack_type smallint NOT NULL CHECK (pack_type IN (4, 6)),
    active    boolean NOT NULL DEFAULT true
);

CREATE TABLE pack (
    pack_id      text PRIMARY KEY,
    job_id       text NOT NULL REFERENCES work_order,
    pallet_id    text REFERENCES pallet,
    pack_type    smallint NOT NULL CHECK (pack_type IN (4, 6)),
    recipe_id    integer NOT NULL REFERENCES pack_recipe,
    status       text NOT NULL DEFAULT 'identified'
                 CHECK (status IN ('identified', 'assembling', 'assembled', 'lidded', 'tested',
                                   'sorted', 'rework', 'scrapped')),
    final_result text CHECK (final_result IN ('OK', 'NG')),
    destination  text CHECK (destination IN ('good', 'ng', 'rework')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE TABLE pack_slot (
    pack_id       text NOT NULL REFERENCES pack,
    slot_no       smallint NOT NULL CHECK (slot_no BETWEEN 1 AND 6),
    cell_id       text NOT NULL UNIQUE REFERENCES cell,  -- 한 셀은 한 팩에만
    source_box_id text REFERENCES cell_box,
    orientation   smallint CHECK (orientation IN (0, 1, 2)),
    placed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pack_id, slot_no)
);

CREATE TABLE pack_inspection (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pack_id         text NOT NULL REFERENCES pack,
    kind            text NOT NULL CHECK (kind IN ('assembly_vision', 'voltage')),
    station_code    text NOT NULL REFERENCES station,
    attempt         smallint NOT NULL DEFAULT 1,
    result          text NOT NULL CHECK (result IN ('OK', 'NG')),
    ng_reason       text CHECK (ng_reason IN ('empty_slot', 'wrong_orientation', 'voltage_low', 'voltage_high')),
    ng_slot         smallint,
    pack_voltage_mv integer,
    method          text CHECK (method IN ('A_probe', 'B_rig')),
    inspected_at    timestamptz NOT NULL,
    UNIQUE (pack_id, kind, attempt)
);

CREATE TABLE rework (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pack_id     text NOT NULL REFERENCES pack,
    reason      text NOT NULL CHECK (reason IN ('empty_slot', 'wrong_orientation', 'voltage_low', 'voltage_high')),
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    result      text CHECK (result IN ('fixed', 'failed'))
);

COMMIT;
