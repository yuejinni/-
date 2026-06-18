"""
tcp/tcp_server.py — asyncio TCP 扫码平台监听（:8888）

兼容两种报文边界：
  1. carno#barcode\r\n
  2. STX + carno#barcode + ETX（02 ... 03）

TCP 数据可能被拆包或粘包，因此按连接维护缓冲区并提取完整帧。
"""
import asyncio
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

STX = 0x02
ETX = 0x03
CRLF = b"\r\n"
MAX_BUFFER_SIZE = 64 * 1024

Frame = Tuple[str, bytes]


def extract_frames(buffer: bytearray) -> Tuple[List[Frame], List[bytes]]:
    """从缓冲区提取完整帧，返回 (有效边界帧, 被丢弃的无效字节)。"""
    frames: List[Frame] = []
    dropped: List[bytes] = []

    while buffer:
        # 海康 STX/ETX 帧。
        if buffer[0] == STX:
            end = buffer.find(ETX, 1)
            if end < 0:
                break
            frames.append(("STX/ETX", bytes(buffer[1:end])))
            del buffer[:end + 1]
            continue

        crlf_at = buffer.find(CRLF)
        stx_at = buffer.find(STX)

        # STX 比下一条 CRLF 更早：STX 前面的内容不是完整帧，丢弃后
        # 从 STX 重新解析。这样噪声不会让海康帧一直卡在缓冲区里。
        if stx_at >= 0 and (crlf_at < 0 or stx_at < crlf_at):
            if stx_at:
                dropped.append(bytes(buffer[:stx_at]))
                del buffer[:stx_at]
            continue

        if crlf_at < 0:
            break

        payload = bytes(buffer[:crlf_at])
        del buffer[:crlf_at + len(CRLF)]
        if payload:
            frames.append(("CRLF", payload))

    return frames, dropped


def process_frame(payload: bytes, protocol: str, db_conn, plc) -> None:
    """校验并处理一条 carno#barcode 业务报文。"""
    msg = payload.decode("utf-8", errors="replace").strip()
    if "#" not in msg:
        logger.warning(
            "[TCP] 无效%s帧（缺少 #）：text=%r hex=%s",
            protocol, msg, payload.hex(" ")
        )
        return

    carno_str, barcode = msg.split("#", 1)
    carno_str = carno_str.strip()
    barcode = barcode.strip()
    if not barcode:
        logger.warning(
            "[TCP] 无效%s帧（条码为空）：hex=%s",
            protocol, payload.hex(" ")
        )
        return

    try:
        carno = int(carno_str)
    except ValueError:
        logger.warning(
            "[TCP] 无效%s帧 carno=%r：hex=%s",
            protocol, carno_str, payload.hex(" ")
        )
        return

    try:
        from core.scan_handler import handle_scan
        handle_scan(db_conn, plc, carno, barcode)
        logger.info(
            "[TCP] 已接收%s帧 carno=%s barcode=%s",
            protocol, carno, barcode
        )
    except Exception:
        logger.exception(
            "[TCP] handle_scan 异常 carno=%s barcode=%s",
            carno, barcode
        )


async def handle_scanner(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter,
                         db_conn, plc):
    peer = writer.get_extra_info("peername")
    logger.info("[TCP] 扫码平台连接：%s", peer)
    buffer = bytearray()

    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break

            buffer.extend(data)
            frames, dropped = extract_frames(buffer)

            for invalid in dropped:
                logger.warning(
                    "[TCP] 丢弃帧边界外字节：hex=%s",
                    invalid.hex(" ")
                )
            for protocol, payload in frames:
                process_frame(payload, protocol, db_conn, plc)

            if len(buffer) > MAX_BUFFER_SIZE:
                logger.warning(
                    "[TCP] 缓冲区超过 %s 字节，清空未成帧数据：hex=%s",
                    MAX_BUFFER_SIZE, bytes(buffer).hex(" ")
                )
                buffer.clear()
    except Exception:
        logger.exception("[TCP] 连接异常 peer=%s", peer)
    finally:
        if buffer:
            logger.warning(
                "[TCP] 连接断开时存在未成帧数据：hex=%s",
                bytes(buffer).hex(" ")
            )
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, AttributeError):
            pass
        logger.info("[TCP] 连接断开：%s", peer)


async def start_tcp_server(db_conn, plc, port: int = 8888):
    """启动 TCP 服务器，持续运行直到进程退出。"""
    server = await asyncio.start_server(
        lambda r, w: handle_scanner(r, w, db_conn, plc),
        "0.0.0.0", port
    )
    logger.info("[TCP] 监听 0.0.0.0:%s（兼容 CRLF 与 STX/ETX）", port)
    async with server:
        await server.serve_forever()
