from __future__ import annotations

from ..config import Config
from .base import PlcError, PlcPort


def open_port(cfg: Config) -> PlcPort:
    """설정의 backend로 PLC 연결을 만든다. 필요한 라이브러리는 그때 불러온다."""
    o = cfg.plc_options
    if cfg.plc_backend == "fake":
        from .fake import FakePlc
        return FakePlc(cfg.mailbox, **o)
    if cfg.plc_backend == "modbus":
        from .modbus import ModbusPlc
        return ModbusPlc(**o)
    if cfg.plc_backend == "mx":
        from .mx import MxPlc
        return MxPlc(**o)
    raise ValueError(f"unknown plc backend '{cfg.plc_backend}' (fake | modbus | mx)")


__all__ = ["PlcError", "PlcPort", "open_port"]
