"""
tcp/tcp_server.py — asyncio TCP 扫码枪监听（:8888）

协议：carno#barcode\r\n
替代原 C# ReceiveClientMsg。
⚠️ 每个连接共享同一 db_conn（asyncio 单线程，无并发问题）。
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def handle_scanner(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter,
                         db_conn, plc):
    peer = writer.get_extra_info('peername')
    logger.info(f"[TCP] 扫码枪连接：{peer}")
    try:
        while True:
            data = await reader.readline()
            if not data:
                break
            msg = data.decode('utf-8', errors='ignore').strip()
            if '#' not in msg:
                continue
            carno_str, barcode = msg.split('#', 1)
            barcode = barcode.strip()
            try:
                carno = int(carno_str.strip())
            except ValueError:
                logger.warning(f"[TCP] 无效 carno: {carno_str!r}")
                continue
            try:
                from core.scan_handler import handle_scan
                handle_scan(db_conn, plc, carno, barcode)
            except Exception as e:
                logger.error(f"[TCP] handle_scan 异常 carno={carno} barcode={barcode}: {e}")
    except Exception as e:
        logger.warning(f"[TCP] 连接异常: {e}")
    finally:
        writer.close()
        logger.info(f"[TCP] 连接断开：{peer}")


async def start_tcp_server(db_conn, plc, port: int = 8888):
    """启动 TCP 服务器，持续运行直到进程退出。"""
    server = await asyncio.start_server(
        lambda r, w: handle_scanner(r, w, db_conn, plc),
        '0.0.0.0', port
    )
    logger.info(f"[TCP] 监听 0.0.0.0:{port}")
    async with server:
        await server.serve_forever()
