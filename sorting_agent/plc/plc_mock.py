"""
plc/plc_mock.py — 开发期 MockPLC

用于在无真实 S7-1200 PLC 的情况下开发和调试。
所有读写操作静默忽略，仅打印日志。
"""
import logging

logger = logging.getLogger(__name__)


class MockPLC:
    """snap7.client.Client 的 Mock 实现，接口与 python-snap7 保持一致。"""

    def connect(self, ip: str, rack: int = 0, slot: int = 1, tcpport: int = 102):
        logger.info(f"[MockPLC] connect({ip}, rack={rack}, slot={slot}) — 已忽略")

    def disconnect(self):
        logger.info("[MockPLC] disconnect() — 已忽略")

    def get_connected(self) -> bool:
        return True

    def db_write(self, db_number: int, start: int, data: bytes):
        logger.debug(f"[MockPLC] db_write(DB{db_number}, offset={start}, len={len(data)}, data={data.hex()})")

    def db_read(self, db_number: int, start: int, size: int) -> bytes:
        logger.debug(f"[MockPLC] db_read(DB{db_number}, offset={start}, size={size}) — 返回全零")
        return bytes(size)
