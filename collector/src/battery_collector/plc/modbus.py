"""Modbus TCP 백엔드 (pymodbus). D 레지스터 = holding register.

실제 MELSEC Q는 MODBUS/TCP 모듈(예: QJ71MT91)의 디바이스 할당에서 holding register를 D 영역에 묶는다.
그 할당이 "HR 0 = D{d_to_hr}" 이면 설정 d_to_hr에 그 D 번호를 쓴다.
"""
from __future__ import annotations

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from .base import PlcError

MAX_READ = 125  # Modbus 한 번에 읽을 수 있는 최대 레지스터 수


class ModbusPlc:
    def __init__(self, host: str = "127.0.0.1", port: int = 502, device_id: int = 1, d_to_hr: int = 0,
                 timeout_s: float = 2.0):
        self.client = ModbusTcpClient(host, port=port, timeout=timeout_s)
        self.device_id = device_id
        self.d_to_hr = d_to_hr

    def _ensure(self) -> None:
        if not self.client.connected and not self.client.connect():
            raise PlcError(f"cannot connect to Modbus server {self.client.comm_params.host}")

    def _hr(self, d: int) -> int:
        a = d - self.d_to_hr
        if a < 0:
            raise ValueError(f"D{d} is below the mapped range (d_to_hr={self.d_to_hr})")
        return a

    def read_words(self, d_start: int, count: int) -> list[int]:
        self._ensure()
        out: list[int] = []
        try:
            for off in range(0, count, MAX_READ):
                n = min(MAX_READ, count - off)
                r = self.client.read_holding_registers(self._hr(d_start + off), count=n, device_id=self.device_id)
                if r.isError():
                    raise PlcError(f"read D{d_start + off}x{n}: {r}")
                out.extend(r.registers)
        except ModbusException as e:
            self.client.close()
            raise PlcError(str(e)) from e
        return out

    def write_word(self, d_addr: int, value: int) -> None:
        self._ensure()
        try:
            r = self.client.write_register(self._hr(d_addr), value & 0xFFFF, device_id=self.device_id)
        except ModbusException as e:
            self.client.close()
            raise PlcError(str(e)) from e
        if r.isError():
            raise PlcError(f"write D{d_addr}: {r}")

    def close(self) -> None:
        self.client.close()
