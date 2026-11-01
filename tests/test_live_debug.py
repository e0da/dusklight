import asyncio
import base64
import importlib.util
import json
import os
import pty
import signal
import socket
import struct
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

SPEC = importlib.util.spec_from_file_location(
    "live_debug", Path(__file__).resolve().parents[1] / "tools/live_debug.py"
)
live_debug = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live_debug)


class WorkbenchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.host = live_debug.Workbench("test-secret", on_event=self.events.append)
        self.server = await self.host.listen(port=0)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def app(self):
        socket = await connect(self.url, additional_headers={"Authorization": "Bearer test-secret"})
        await socket.send(json.dumps({"type": "hello", "version": 1}))
        await asyncio.wait_for(self.host.ready.wait(), 1)
        return socket

    async def test_auth_rejected_before_hello(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, [("Authorization", "Bearer test-secret"), ("Authorization", "Bearer test-secret")]):
            with self.subTest(headers=headers):
                with self.assertRaises(InvalidStatus) as caught:
                    await connect(self.url, additional_headers=headers)
                self.assertEqual(caught.exception.response.status_code, 401)
        self.assertFalse(self.host.ready.is_set())

    async def test_hello_version_and_malformed(self):
        for raw in ('{"type":"hello","version":2}', '{"type":"hello","version":true}', '[]', '{', b'hello'):
            async with connect(self.url, additional_headers={"Authorization": "Bearer test-secret"}) as app:
                await app.send(raw)
                with self.assertRaises(ConnectionClosed) as caught:
                    await app.recv()
                self.assertEqual(caught.exception.rcvd.code, 1008)
        self.assertFalse(self.host.ready.is_set())

    async def test_correlation_events_and_remote_error(self):
        app = await self.app()
        async with app:
            ping = asyncio.create_task(self.host.request("ping"))
            command = asyncio.create_task(self.host.request("command", {"line": "help"}))
            first = json.loads(await app.recv())
            second = json.loads(await app.recv())
            self.assertEqual(first["method"], "ping")
            self.assertEqual(second["params"], {"line": "help"})
            await app.send(json.dumps({"type": "response", "id": second["id"], "result": {"lines": ["help text"], "truncated": False, "completed": True}}))
            await app.send(json.dumps({"type": "event", "event": "telemetry", "seq": 3, "dropped": 1, "data": {"frame": 42}}))
            await app.send(json.dumps({"type": "response", "id": first["id"], "result": {"pong": True}}))
            self.assertEqual((await ping)["pong"], True)
            self.assertEqual((await command)["lines"], ["help text"])
            self.assertEqual(self.events[0]["data"]["frame"], 42)
            request = asyncio.create_task(self.host.request("unsubscribe"))
            message = json.loads(await app.recv())
            await app.send(json.dumps({"type": "response", "id": message["id"], "error": {"code": "busy", "message": "try later"}}))
            with self.assertRaises(live_debug.RemoteError):
                await request

    async def test_disconnect_unblocks_pending_and_reconnects(self):
        app = await self.app()
        task = asyncio.create_task(self.host.request("ping"))
        await app.recv()
        await app.close()
        with self.assertRaises(ConnectionError):
            await task
        self.assertFalse(self.host.ready.is_set())
        async with await self.app():
            self.assertTrue(self.host.ready.is_set())

    async def test_malformed_response_fails_pending(self):
        for malformed in ({"type": "response", "id": "unknown", "result": {}}, {"type": "event", "event": "telemetry", "seq": -1, "dropped": 0, "data": {}}, {"type": "response", "id": "ID", "result": {}, "error": {}}):
            app = await self.app()
            task = asyncio.create_task(self.host.request("ping"))
            request = json.loads(await app.recv())
            if malformed.get("id") == "ID":
                malformed["id"] = request["id"]
            await app.send(json.dumps(malformed))
            with self.assertRaises(ConnectionError):
                await task
            await app.wait_closed()
            self.assertEqual(app.close_code, 1008)

    async def test_second_app_cannot_take_over(self):
        app = await self.app()
        async with app:
            async with connect(self.url, additional_headers={"Authorization": "Bearer test-secret"}) as other:
                await other.send('{"type":"hello","version":1}')
                with self.assertRaises(ConnectionClosed):
                    await other.recv()
                self.assertEqual(other.close_code, 1013)
            self.assertIsNotNone(self.host.connection)

    async def test_request_limits_and_timeout(self):
        app = await self.app()
        async with app:
            for method, params in (("command", {"line": "x" * 4097}), ("command", {"line": "é" * 2049}), ("subscribe", {"hz": 11}), ("subscribe", {"hz": True}), ("invalid", {})):
                with self.assertRaises(ValueError):
                    await self.host.request(method, params)
            with self.assertRaises(asyncio.TimeoutError):
                await self.host.request("ping", timeout=0.01)
            self.assertEqual(self.host.pending, {})

    @staticmethod
    def png(width=2, height=3, padding=0):
        return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"x" * padding

    @staticmethod
    def capture_chunk(data, offset=0, total=None, done=True):
        return {"captureId": "capture-1", "state": "ready", "format": "png",
                "width": 2, "height": 3, "totalBytes": len(data) if total is None else total,
                "offset": offset, "data": base64.b64encode(data).decode(), "done": done}

    async def test_screenshot_socket_transfer_is_atomic_private_and_not_recorded(self):
        data = self.png(padding=9000)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            record = Path(directory) / "record.jsonl"
            recorder = live_debug.Recorder(record, 65536)
            self.host.recorder = recorder
            app = await self.app()
            async with app:
                transfer = asyncio.create_task(self.host.screenshot(path))
                requests = []
                replies = [{"captureId": "capture-1", "state": "pending"},
                           {"captureId": "capture-1", "state": "pending"},
                           self.capture_chunk(data[:8192], total=len(data), done=False),
                           self.capture_chunk(data[8192:], offset=8192, total=len(data)),
                           {"cancelled": True}]
                for reply in replies:
                    request = json.loads(await asyncio.wait_for(app.recv(), 1))
                    self.assertEqual(request["method"], "screenshot")
                    requests.append(request["params"])
                    if reply.get("done") is False:
                        self.assertFalse(path.exists())
                    await app.send(json.dumps({"type": "response", "id": request["id"], "result": reply}))
                result = await transfer
                self.assertEqual(result, {"path": str(path), "width": 2, "height": 3, "bytes": len(data)})
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(requests[-1], {"action": "cancel", "captureId": "capture-1"})
                self.assertEqual(requests[-2]["offset"], 8192)
                self.assertEqual(self.host.unrecorded, set())
            recorder.close()
            self.assertEqual(record.read_bytes(), b"")
            self.assertEqual(sorted(file.name for file in Path(directory).iterdir()), ["image.png", "record.jsonl"])

    async def test_screenshot_malformed_chunks_preserve_existing_file_and_cancel(self):
        valid = self.capture_chunk(self.png())
        malformed = [
            {"offset": 1}, {"offset": True}, {"width": True}, {"width": 4097},
            {"totalBytes": live_debug.MAX_CAPTURE_BYTES + 1}, {"totalBytes": 0},
            {"data": "@@@"}, {"data": ""}, {"data": base64.b64encode(b"x" * 8193).decode()},
            {"done": False}, {"done": 1}, {"captureId": "other"}, {"format": "jpeg"},
            {"state": "invalid"}, {"width": 1},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(b"previous image")
            for bad in malformed:
                with self.subTest(bad=bad):
                    request = AsyncMock(side_effect=[
                        {"captureId": "capture-1", "state": "pending"},
                        {**valid, **bad}, {"cancelled": True},
                    ])
                    with patch.object(self.host, "request", request):
                        with self.assertRaises(live_debug.ProtocolError):
                            await self.host.screenshot(path)
                    self.assertEqual(path.read_bytes(), b"previous image")
                    self.assertEqual(list(Path(directory).iterdir()), [path])
                    self.assertEqual(request.await_args.args, ("screenshot", {"action": "cancel", "captureId": "capture-1"}))

    async def test_screenshot_rejects_metadata_drift(self):
        data = self.png(padding=9000)
        chunks = [self.capture_chunk(data[:8192], total=len(data), done=False),
                  self.capture_chunk(data[8192:], offset=8192, total=len(data))]
        chunks[1]["height"] = 4
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            with patch.object(self.host, "request", AsyncMock(side_effect=[
                    {"captureId": "capture-1", "state": "pending"}, *chunks, {"cancelled": True}])):
                with self.assertRaises(live_debug.ProtocolError):
                    await self.host.screenshot(path)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_screenshot_disconnect_removes_partial_and_does_not_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            app = await self.app()
            transfer = asyncio.create_task(self.host.screenshot(path))
            request = json.loads(await app.recv())
            await app.send(json.dumps({"type": "response", "id": request["id"], "result": {"captureId": "capture-1", "state": "pending"}}))
            await app.recv()
            await app.close()
            with self.assertRaises(ConnectionError):
                await transfer
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_screenshot_timeout_cancels_and_cleans_up(self):
        async def pending(method, params, timeout=10):
            if params["action"] == "cancel":
                return {"cancelled": True}
            return {"captureId": "capture-1", "state": "pending"}
        request = AsyncMock(side_effect=pending)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(self.host, "request", request):
                with self.assertRaises(asyncio.TimeoutError):
                    await self.host.screenshot(Path(directory) / "image.png", timeout=0.01)
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(request.await_args.args[1]["action"], "cancel")

    async def test_screenshot_deadline_override_and_invalid_limits(self):
        request = AsyncMock()
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), patch.object(self.host, "request", request):
                with self.assertRaisesRegex(ValueError, "positive and finite"):
                    await self.host.screenshot("unused.png", timeout=timeout)
        request.assert_not_awaited()
        self.host.ready.set()
        screenshot = AsyncMock(return_value={"path": "image.png", "bytes": 123})
        args = SimpleNamespace(wait=1, subscribe=None, command=[], ui=None,
                               screenshot="image.png", screenshot_timeout=75)
        with patch.object(self.host, "screenshot", screenshot), patch("builtins.print"):
            await live_debug.console(self.host, args)
        screenshot.assert_awaited_once_with("image.png", timeout=75)

    async def test_ui_routing_and_capture_request_validation(self):
        app = await self.app()
        async with app:
            task = asyncio.create_task(self.host.request("ui", {"action": "channel"}))
            request = json.loads(await app.recv())
            self.assertEqual(request["method"], "ui")
            self.assertEqual(request["params"], {"action": "channel"})
            await app.send(json.dumps({"type": "response", "id": request["id"], "result": {"visible": True}}))
            self.assertEqual(await task, {"visible": True})
            for method, params in (("ui", {"action": "invalid"}), ("screenshot", {"action": "invalid"}),
                                   ("screenshot", {"action": "read", "captureId": "x", "offset": True}),
                                   ("screenshot", {"action": "read", "captureId": "x", "offset": -1}),
                                   ("screenshot", {"action": "cancel", "captureId": ""})):
                with self.subTest(params=params), self.assertRaises(ValueError):
                    await self.host.request(method, params)

    async def test_one_shot_ui_then_screenshot_console_prints_only_metadata(self):
        self.host.ready.set()
        request = AsyncMock(return_value={"visible": True})
        screenshot = AsyncMock(return_value={"path": "image.png", "bytes": 123})
        args = SimpleNamespace(wait=1, subscribe=None, command=[], ui="channel", screenshot="image.png")
        with patch.object(self.host, "request", request), patch.object(self.host, "screenshot", screenshot), patch("builtins.print") as output:
            await live_debug.console(self.host, args)
        request.assert_awaited_once_with("ui", {"action": "channel"})
        screenshot.assert_awaited_once_with("image.png", timeout=120)
        self.assertIn('{"path": "image.png", "bytes": 123}', [call.args[0] for call in output.call_args_list])

    def test_recording_is_bounded_private_and_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.jsonl"
            recorder = live_debug.Recorder(path, 40)
            recorder.write({"ok": True})
            recorder.write({"large": "x" * 50})
            recorder.close()
            self.assertEqual(json.loads(path.read_text()), {"ok": True})
            self.assertLessEqual(path.stat().st_size, 40)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.chmod(0o644)
            recorder = live_debug.Recorder(path, 15)
            recorder.write({"ok": True})
            recorder.close()
            self.assertLessEqual(path.stat().st_size, 15)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_console(self, terminal, exit_mode):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        master = slave = None
        if terminal:
            master, slave = pty.openpty()
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).resolve().parents[1] / "tools/live_debug.py"),
            "--port", str(port), "--wait", "5",
            env={**os.environ, "DUSK_DEBUG_TOKEN": "console-secret"},
            stdin=slave if terminal else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        if slave is not None:
            os.close(slave)
        try:
            listening = await asyncio.wait_for(process.stderr.readline(), 5)
            self.assertIn(b"Listening", listening)
            async with connect(f"ws://127.0.0.1:{port}", additional_headers={"Authorization": "Bearer console-secret"}) as app:
                await app.send('{"type":"hello","version":1}')
                self.assertIn(b"App connected", await asyncio.wait_for(process.stderr.readline(), 2))
                self.assertIn(b"Enter a game command", await asyncio.wait_for(process.stderr.readline(), 2))
                # A blocked stdin reader must not stall the WebSocket receive task.
                await app.send(json.dumps({"type": "event", "event": "telemetry", "seq": 1, "dropped": 0, "data": {"frame": 123}}))
                event = json.loads(await asyncio.wait_for(process.stdout.readline(), 2))
                self.assertEqual(event["data"]["frame"], 123)
                if terminal:
                    os.write(master, b"/ping\n")
                else:
                    process.stdin.write(b"x" * 20000 + b"\n/ping\n")
                    await process.stdin.drain()
                    self.assertIn(b"Input line too long", await asyncio.wait_for(process.stderr.readline(), 2))
                request = json.loads(await asyncio.wait_for(app.recv(), 2))
                self.assertEqual(request["method"], "ping")
                await app.send(json.dumps({"type": "response", "id": request["id"], "result": {"pong": True}}))
                self.assertEqual(json.loads(await asyncio.wait_for(process.stdout.readline(), 2)), {"pong": True})
                if exit_mode == "interrupt":
                    process.send_signal(signal.SIGINT)
                elif exit_mode == "eof":
                    if terminal:
                        os.write(master, b"\x04")
                    else:
                        process.stdin.close()
                else:
                    os.write(master, b"/quit\n")
                self.assertEqual(await asyncio.wait_for(process.wait(), 3), 0)
                self.assertEqual(await process.stderr.read(), b"")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if master is not None:
                os.close(master)

    async def test_real_pty_ping_telemetry_and_quit(self):
        await self.exercise_console(True, "quit")

    async def test_real_pty_eof_exits(self):
        await self.exercise_console(True, "eof")

    async def test_real_pty_interrupt_exits_without_stdin(self):
        await self.exercise_console(True, "interrupt")

    async def test_pipe_long_line_recovers_and_eof_exits(self):
        await self.exercise_console(False, "eof")


if __name__ == "__main__":
    unittest.main()
