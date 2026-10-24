import io
import socket
import struct
import unittest

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nexus.proto.framing import MAX_FRAME, FrameError, encode, read_frame, recv_exact_from


class TestEncode(unittest.TestCase):
    def test_round_trip(self):
        msg = {"v": 1, "id": "abc", "type": "ping", "body": {"timestamp": 42}}
        data = encode(msg)
        self.assertIsInstance(data, bytes)
        self.assertGreater(len(data), 4)

    def test_json_compact(self):
        msg = {"key": "val", "num": 1}
        data = encode(msg)
        # Extract body (skip 4-byte header)
        length = struct.unpack(">I", data[:4])[0]
        body = data[4:]
        self.assertEqual(len(body), length)
        # Verify compact JSON (no spaces)
        self.assertNotIn(b" ", body)


class TestReadFrame(unittest.TestCase):
    def test_round_trip(self):
        msg = {"v": 1, "id": "0" * 32, "type": "ping", "body": {}}
        data = encode(msg)

        # Simulate recv_exact
        buf = bytearray(data)
        pos = 0

        def recv_exact(n):
            nonlocal pos
            result = bytes(buf[pos : pos + n])
            pos += n
            return result

        result = read_frame(recv_exact)
        self.assertEqual(result, msg)

    def test_zero_size_rejected(self):
        data = struct.pack(">I", 0)

        def recv_exact(n):
            return data[:n]

        with self.assertRaises(FrameError) as ctx:
            read_frame(recv_exact)
        self.assertIn("zero-size", str(ctx.exception).lower())

    def test_oversized_rejected(self):
        data = struct.pack(">I", 1024 * 1024 + 1)

        def recv_exact(n):
            return data[:n]

        with self.assertRaises(FrameError) as ctx:
            read_frame(recv_exact)
        self.assertIn("exceeds maximum", str(ctx.exception).lower())

    def test_non_utf8_payload(self):
        body = b"\xff\xfe"
        data = struct.pack(">I", len(body)) + body

        def recv_exact(n):
            return data[:n]

        with self.assertRaises(FrameError) as ctx:
            read_frame(recv_exact)
        self.assertIn("invalid frame body", str(ctx.exception).lower())

    def test_invalid_json(self):
        body = b"not json"
        data = struct.pack(">I", len(body)) + body

        def recv_exact(n):
            return data[:n]

        with self.assertRaises(FrameError) as ctx:
            read_frame(recv_exact)
        self.assertIn("invalid frame body", str(ctx.exception).lower())

    def test_json_list_rejected(self):
        body = b"[1,2,3]"
        data = struct.pack(">I", len(body)) + body
        pos = [0]

        def recv_exact(n):
            nonlocal pos
            result = data[pos[0] : pos[0] + n]
            pos[0] += n
            return result

        with self.assertRaises(FrameError) as ctx:
            read_frame(recv_exact)
        self.assertIn("json object", str(ctx.exception).lower())

    def test_mid_stream_eof(self):
        body = b"hello world"
        data = struct.pack(">I", len(body)) + body

        # Simulate partial read then EOF
        call_count = [0]

        def recv_exact(n):
            call_count[0] += 1
            if call_count[0] == 1:
                return data[:n]  # header
            # body: only return partial
            return data[4 : 4 + n // 2]

        with self.assertRaises(FrameError) as ctx:
            read_frame(recv_exact)
        self.assertIn("truncated", str(ctx.exception).lower())


class TestRecvExactFrom(unittest.TestCase):
    def test_delivers_exact_bytes(self):
        class FakeSocket:
            def __init__(self, data):
                self.data = data
                self.pos = 0

            def recv(self, n):
                chunk = self.data[self.pos : self.pos + n]
                self.pos += n
                return chunk

        sock = FakeSocket(b"hello world")
        recv_exact = recv_exact_from(sock)

        self.assertEqual(recv_exact(5), b"hello")
        self.assertEqual(recv_exact(6), b" world")

    def test_partial_chunks(self):
        class FakeSocket:
            def __init__(self, data):
                self.data = data
                self.pos = 0

            def recv(self, n):
                # Deliver 1 byte at a time
                if self.pos < len(self.data):
                    result = self.data[self.pos : self.pos + 1]
                    self.pos += 1
                    return result
                return b""

        sock = FakeSocket(b"test")
        recv_exact = recv_exact_from(sock)

        self.assertEqual(recv_exact(4), b"test")

    def test_connection_closed(self):
        class FakeSocket:
            def recv(self, n):
                return b""

        sock = FakeSocket()
        recv_exact = recv_exact_from(sock)

        with self.assertRaises(EOFError) as ctx:
            recv_exact(10)
        self.assertIn("connection closed", str(ctx.exception).lower())


class TestEncodeLimit(unittest.TestCase):
    def test_encode_too_large(self):
        with self.assertRaises(FrameError):
            encode({"x": "a" * MAX_FRAME})


if __name__ == "__main__":
    unittest.main()