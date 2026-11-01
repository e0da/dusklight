import asyncio
import importlib.util
import json
import os
import pty
import signal
import socket
import sys
from pathlib import Path
import tempfile
import unittest

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
