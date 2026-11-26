"""PLC 접근 인터페이스. 수집기는 이것만 안다.

백엔드:
    fake  PLC 없이 개발·시험 (메일박스에 가짜 셀 이벤트를 쓴다)
    modbus  Modbus TCP 서버 (실제 MELSEC Q + MODBUS 모듈, 또는 Modbus로 열어 둔 브리지)
    mx    MX Component (GX Simulator2). Windows 전용

브리지 프로세스 안에 넣을 때는 브리지가 이미 가진 PLC 연결로 이 두 메서드를 구현해서 넘긴다
("PLC에 쓰는 프로그램은 브리지 하나" 규칙, digital-twin-a docs/interface.md).
"""
from __future__ import annotations

from typing import Protocol


class PlcPort(Protocol):
    def read_words(self, d_start: int, count: int) -> list[int]:
        """D{d_start}부터 count 워드. 각 값은 0~65535."""
        ...

    def write_word(self, d_addr: int, value: int) -> None:
        """D{d_addr}에 1워드."""
        ...

    def close(self) -> None: ...


class PlcError(RuntimeError):
    """통신 실패. 수집기는 잠시 뒤 다시 시도한다."""
