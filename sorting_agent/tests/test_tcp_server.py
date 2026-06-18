import asyncio
import unittest
import sys
from types import ModuleType
from unittest.mock import Mock, patch

from tcp.tcp_server import extract_frames, handle_scanner, process_frame


class ExtractFramesTests(unittest.TestCase):
    def test_crlf_frame_can_arrive_in_fragments(self):
        buffer = bytearray(b"1#230921")
        frames, dropped = extract_frames(buffer)
        self.assertEqual([], frames)
        self.assertEqual([], dropped)

        buffer.extend(b"C029015\r\n")
        frames, dropped = extract_frames(buffer)
        self.assertEqual([("CRLF", b"1#230921C029015")], frames)
        self.assertEqual([], dropped)
        self.assertEqual(bytearray(), buffer)

    def test_stx_etx_frame_can_arrive_in_fragments(self):
        buffer = bytearray(b"\x021#230921")
        frames, dropped = extract_frames(buffer)
        self.assertEqual([], frames)
        self.assertEqual([], dropped)

        buffer.extend(b"C029015\x03")
        frames, dropped = extract_frames(buffer)
        self.assertEqual([("STX/ETX", b"1#230921C029015")], frames)
        self.assertEqual([], dropped)
        self.assertEqual(bytearray(), buffer)

    def test_mixed_sticky_packets(self):
        buffer = bytearray(
            b"1#CRLF001\r\n\x022#STX002\x033#CRLF003\r\n"
        )
        frames, dropped = extract_frames(buffer)
        self.assertEqual(
            [
                ("CRLF", b"1#CRLF001"),
                ("STX/ETX", b"2#STX002"),
                ("CRLF", b"3#CRLF003"),
            ],
            frames,
        )
        self.assertEqual([], dropped)
        self.assertEqual(bytearray(), buffer)

    def test_noise_before_stx_is_reported_and_discarded(self):
        buffer = bytearray(b"noise\x021#ABC\x03")
        frames, dropped = extract_frames(buffer)
        self.assertEqual([("STX/ETX", b"1#ABC")], frames)
        self.assertEqual([b"noise"], dropped)


class ProcessFrameTests(unittest.TestCase):
    def setUp(self):
        self.handle_scan = Mock()
        self.scan_handler_module = ModuleType("core.scan_handler")
        self.scan_handler_module.handle_scan = self.handle_scan
        self.module_patch = patch.dict(
            sys.modules,
            {"core.scan_handler": self.scan_handler_module},
        )
        self.module_patch.start()

    def tearDown(self):
        self.module_patch.stop()

    def test_valid_frame_calls_existing_business_handler(self):
        db_conn = object()
        plc = object()
        process_frame(b"1#230921C029015", "STX/ETX", db_conn, plc)
        self.handle_scan.assert_called_once_with(
            db_conn, plc, 1, "230921C029015"
        )

    def test_invalid_frame_is_ignored(self):
        process_frame(b"not-a-valid-frame", "CRLF", object(), object())
        self.handle_scan.assert_not_called()


class TcpIntegrationTests(unittest.TestCase):
    def test_socket_accepts_both_protocols_and_fragmented_data(self):
        async def scenario():
            completed = asyncio.Event()

            async def handler(reader, writer):
                await handle_scanner(reader, writer, object(), object())
                completed.set()

            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]

            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port
                )
                del reader
                writer.write(b"1#CRLF")
                await writer.drain()
                writer.write(b"001\r\n\x022#STX")
                await writer.drain()
                writer.write(b"002\x03")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                await asyncio.wait_for(completed.wait(), timeout=1)
            finally:
                server.close()
                await server.wait_closed()

        with patch("tcp.tcp_server.process_frame") as process:
            asyncio.run(scenario())

        self.assertEqual(
            [
                ((b"1#CRLF001", "CRLF", unittest.mock.ANY,
                  unittest.mock.ANY), {}),
                ((b"2#STX002", "STX/ETX", unittest.mock.ANY,
                  unittest.mock.ANY), {}),
            ],
            process.call_args_list,
        )


if __name__ == "__main__":
    unittest.main()
