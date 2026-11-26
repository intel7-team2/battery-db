"""실제 Modbus TCP 서버(pymodbus)를 띄워 ModbusPlc + 수집기를 시험한다.

서버 메모리를 PLC D 영역으로 보고, 가짜 PLC 로직으로 레코드를 쓴다.
"""
import socket
import threading
import time
from datetime import datetime, timezone

import pytest

pytest.importorskip("pymodbus")
from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext  # noqa: E402
from pymodbus.server import ServerStop, StartTcpServer  # noqa: E402

from battery_collector.collector import Collector  # noqa: E402
from battery_collector.outbox import Outbox  # noqa: E402
from battery_collector.plc.fake import FakePlc  # noqa: E402
from battery_collector.plc.modbus import ModbusPlc  # noqa: E402

D_TO_HR = 2000  # HR 0 = D2000 으로 매핑된 모듈이라고 가정


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def server():
    block = ModbusSequentialDataBlock(1, [0] * 2000)  # pymodbus 3.16: 주소는 1부터 (내부에서 -1)
    ctx = ModbusServerContext(devices=ModbusDeviceContext(hr=block), single=True)
    port = free_port()
    t = threading.Thread(target=StartTcpServer, kwargs={"context": ctx, "address": ("127.0.0.1", port)},
                         daemon=True)
    t.start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.05)
    yield port
    ServerStop()


class PlcOverModbus(FakePlc):
    """가짜 PLC 로직이 Modbus 서버 메모리에 쓰도록 (래더 대신)."""

    def __init__(self, mb, port):
        super().__init__(mb, cells_per_min=0.0001)
        self.io = ModbusPlc("127.0.0.1", port, d_to_hr=D_TO_HR)

    def push(self, rec):
        # 현재 메모리를 서버에서 가져와 FakePlc.push로 계산한 뒤 바뀐 워드만 쓴다
        base, size = self.mb.base, self.mb.size
        cur = self.io.read_words(base, size)
        self.mem = {base + i: v for i, v in enumerate(cur)}
        ok = super().push(rec)
        for i in range(size):
            if self.mem.get(base + i, 0) != cur[i]:
                self.io.write_word(base + i, self.mem[base + i])
        return ok


def test_collect_over_modbus(mb, tmp_path, server):
    writer = PlcOverModbus(mb, server)
    port = ModbusPlc("127.0.0.1", server, d_to_hr=D_TO_HR)
    ob = Outbox(tmp_path / "ob.sqlite3")
    col = Collector(mb, "2A", port, ob, 5, clock=lambda: datetime(2026, 11, 26, tzinfo=timezone.utc))
    col.poll_once()
    for i in range(1, 21):
        writer.push({"event": 2, "station": 2, "cell_no": 1000 + i, "lot": 7})
        if i % 5 == 0:
            col.poll_once()
    ev = ob.next_batch(100)
    assert [e["seq"] for e in ev] == list(range(1, 21))
    assert ev[0]["entity_id"] == "R0005-001001" and ev[0]["payload"]["lot"] == 7
    assert port.read_words(mb.base + mb.read_seq, 1) == [20]  # ACK가 Modbus로 써짐
    port.close()
    writer.io.close()
