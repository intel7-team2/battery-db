"""MX Component 백엔드 (GX Simulator2 / 실제 Q PLC). Windows + pywin32 필요.

**시험 안 됨:** 이 PC에 MX Component가 없다. 처음 쓸 때 확인할 것:
- MX Component 버전. v4는 32비트 COM이라 32비트 Python이 필요할 수 있다 (interface.md).
- 통신 설정 유틸리티에서 논리 국번(logical station)에 GX Simulator2를 잡아 둘 것.
- 브리지가 이미 같은 시뮬레이터에 붙어 있으면 이 백엔드 대신 브리지 안에서 수집기를 돌린다
  (PlcPort를 브리지의 연결로 구현해서 넘김). PLC에 쓰는 프로그램은 브리지 하나여야 한다.
"""
from __future__ import annotations

from .base import PlcError


class MxPlc:
    def __init__(self, logical_station: int = 1):
        try:
            import win32com.client  # type: ignore
        except ImportError as e:
            raise PlcError("MX backend needs pywin32 (pip install pywin32) on Windows") from e
        self.act = win32com.client.Dispatch("ActUtlType.ActUtlType")
        self.act.ActLogicalStationNumber = logical_station
        rc = self.act.Open()
        if rc != 0:
            raise PlcError(f"MX Open failed: 0x{rc & 0xFFFFFFFF:08X}")

    def read_words(self, d_start: int, count: int) -> list[int]:
        # ReadDeviceBlock2: 16비트 단위. pywin32는 (반환 코드, 값 목록)으로 돌려준다
        try:
            rc, data = self.act.ReadDeviceBlock2(f"D{d_start}", count, [0] * count)
        except Exception as e:  # pywintypes.com_error 등
            raise PlcError(f"MX read D{d_start}x{count}: {e}") from e
        if rc != 0:
            raise PlcError(f"MX read D{d_start}x{count} failed: 0x{rc & 0xFFFFFFFF:08X}")
        return [v & 0xFFFF for v in data]

    def write_word(self, d_addr: int, value: int) -> None:
        v = value & 0xFFFF
        if v >= 0x8000:
            v -= 0x10000  # COM short
        try:
            rc = self.act.SetDevice2(f"D{d_addr}", v)
        except Exception as e:
            raise PlcError(f"MX write D{d_addr}: {e}") from e
        if isinstance(rc, tuple):
            rc = rc[0]
        if rc != 0:
            raise PlcError(f"MX write D{d_addr} failed: 0x{rc & 0xFFFFFFFF:08X}")

    def close(self) -> None:
        try:
            self.act.Close()
        except Exception:
            pass
