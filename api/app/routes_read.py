"""조회 엔드포인트 (scope read). 대부분 schema/02_views.sql 뷰를 그대로 돌려준다.

단위는 DB와 같다: 전압 mV, 전류 mA, 내부저항 mΩ, 시각 UTC(ISO 8601).
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from . import db
from .auth import require

router = APIRouter(dependencies=[Depends(require("read"))], tags=["read"])

Line = Literal["2A", "PACK", "2B"]


def _one(row: dict | None, what: str) -> dict:
    if row is None:
        raise HTTPException(404, f"{what} not found")
    return row


@router.get("/stations")
async def stations(line: Line | None = None):
    return await db.fetch(
        "SELECT * FROM station WHERE $1::text IS NULL OR line = $1 ORDER BY line, sort_order", line)


@router.get("/criteria/current")
async def criteria_current():
    return _one(await db.fetchrow(
        "SELECT * FROM criteria_set ORDER BY valid_from DESC, id DESC LIMIT 1"), "criteria")


@router.get("/runs")
async def runs(line: Line | None = None, limit: int = Query(50, ge=1, le=500)):
    return await db.fetch(
        "SELECT * FROM sim_run WHERE $1::text IS NULL OR line = $1 ORDER BY run_id DESC LIMIT $2",
        line, limit)


@router.get("/runs/{run_id}")
async def run(run_id: int):
    """시험 1회 + 수율(v_run_yield) + 세트 편차 비교(v_set_spread_compare)."""
    r = _one(await db.fetchrow("SELECT * FROM sim_run WHERE run_id = $1", run_id), "run")
    r["yield"] = await db.fetchrow("SELECT * FROM v_run_yield WHERE run_id = $1", run_id)
    r["set_spread"] = await db.fetchrow("SELECT * FROM v_set_spread_compare WHERE run_id = $1", run_id)
    return r


@router.get("/runs/{run_id}/testers")
async def run_testers(run_id: int):
    """검사기 레인별 검사 수·NG율·평균 검사 시간 (v_tester_stats)."""
    return await db.fetch(
        "SELECT * FROM v_tester_stats WHERE run_id = $1 ORDER BY station_code", run_id)


@router.get("/runs/{run_id}/equipment")
async def run_equipment(run_id: int):
    """설비 상태별 누적 초 (v_equipment_time)."""
    return await db.fetch(
        "SELECT * FROM v_equipment_time WHERE run_id = $1 ORDER BY station_code, state", run_id)


@router.get("/cells/{cell_id}")
async def cell(cell_id: str):
    """셀 1개 + 검사 이력(시도 순) + 최종 전압 판정."""
    c = _one(await db.fetchrow("SELECT * FROM cell WHERE cell_id = $1", cell_id), "cell")
    c["inspections"] = await db.fetch(
        "SELECT * FROM cell_inspection WHERE cell_id = $1 ORDER BY measured_at, id", cell_id)
    c["electrical_latest"] = await db.fetchrow(
        "SELECT * FROM v_cell_electrical_latest WHERE cell_id = $1", cell_id)
    return c


@router.get("/boxes")
async def boxes(run_id: int | None = None,
                status: Literal["filling", "full", "handed_over", "consumed"] | None = None,
                grade: Literal["A", "B", "C"] | None = None,
                limit: int = Query(100, ge=1, le=1000)):
    """박스 목록 + 세트 편차 (v_box_spread)."""
    return await db.fetch(
        """SELECT * FROM v_box_spread
           WHERE ($1::int IS NULL OR run_id = $1) AND ($2::text IS NULL OR status = $2)
             AND ($3::text IS NULL OR grade = $3)
           ORDER BY packed_at DESC NULLS FIRST, box_id LIMIT $4""",
        run_id, status, grade, limit)


@router.get("/boxes/{box_id}")
async def box(box_id: str):
    b = _one(await db.fetchrow("SELECT * FROM v_box_spread WHERE box_id = $1", box_id), "box")
    b["cells"] = await db.fetch(
        """SELECT c.cell_id, c.box_slot, c.lot_code, c.grade, c.status, e.ocv_mv, e.ir_mohm, e.attempt
           FROM cell c LEFT JOIN v_cell_electrical_latest e USING (cell_id)
           WHERE c.box_id = $1 ORDER BY c.box_slot""", box_id)
    b["handoffs"] = await db.fetch(
        "SELECT * FROM supply_handoff WHERE box_id = $1 ORDER BY requested_at", box_id)
    return b


@router.get("/lots")
async def lots():
    return await db.fetch("SELECT * FROM v_lot_status ORDER BY lot_code")


@router.get("/alarms/open")
async def alarms_open():
    return await db.fetch("SELECT * FROM v_alarm_open")


@router.get("/packs/{pack_id}/trace")
async def pack_trace(pack_id: str):
    """팩 → 슬롯 → 셀 → LOT·박스·2A 검사값 (v_pack_trace)."""
    rows = await db.fetch("SELECT * FROM v_pack_trace WHERE pack_id = $1 ORDER BY slot_no", pack_id)
    if not rows:
        raise HTTPException(404, "pack not found")
    return rows
