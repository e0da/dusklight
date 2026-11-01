#!/usr/bin/env python3
"""Authenticated host workbench for an outgoing Dusklight debug connection."""

import argparse
import asyncio
import base64
import binascii
import concurrent.futures
from contextlib import suppress
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import shlex
import struct
import sys
import threading
import tempfile
from http import HTTPStatus

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

MAX_MESSAGE = 65536
MAX_COMMAND = 4096
MAX_PENDING = 32
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_CAPTURE_CHUNK = 8192
UI_ACTIONS = ("channel", "menu", "close", "status")
DEFAULT_SCREENSHOT_TIMEOUT = 120


class ProtocolError(Exception):
    pass


class RemoteError(Exception):
    def __init__(self, error):
        self.code = error["code"]
        super().__init__(f"{self.code}: {error['message']}")


class Recorder:
    """Append JSONL until a fixed byte budget is exhausted."""

    def __init__(self, path, limit):
        self.file = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a")
        os.fchmod(self.file.fileno(), 0o600)
        self.remaining = max(0, limit - os.fstat(self.file.fileno()).st_size)

    def write(self, message):
        line = json.dumps(message, ensure_ascii=True) + "\n"
        size = len(line.encode("utf-8"))
        if size <= self.remaining:
            self.file.write(line)
            self.file.flush()
            self.remaining -= size

    def close(self):
        self.file.close()


class Workbench:
    def __init__(self, token, recorder=None, on_event=None):
        if not token or len(token) > 1024 or any(c.isspace() for c in token):
            raise ValueError("token must contain 1..1024 non-whitespace characters")
        self.token = token
        self.recorder = recorder
        self.on_event = on_event
        self.connection = None
        self.ready = asyncio.Event()
        self.pending = {}
        self.unrecorded = set()
        self.next_id = 0

    def authorize(self, connection, request):
        values = request.headers.get_all("Authorization")
        if len(values) != 1 or not hmac.compare_digest(
            values[0].encode(), ("Bearer " + self.token).encode()
        ):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
        return None

    async def listen(self, host="127.0.0.1", port=8765):
        return await serve(
            self.handle, host, port, process_request=self.authorize,
            max_size=MAX_MESSAGE, max_queue=8, write_limit=MAX_MESSAGE,
            compression=None, close_timeout=2,
        )

    @staticmethod
    def reject_constant(value):
        raise ValueError("non-finite number")

    @staticmethod
    def decode(raw):
        if not isinstance(raw, str):
            raise ProtocolError("expected text JSON")
        try:
            value = json.loads(raw, parse_constant=Workbench.reject_constant)
        except (ValueError, RecursionError) as error:
            raise ProtocolError("invalid JSON") from error
        if not isinstance(value, dict):
            raise ProtocolError("expected JSON object")
        return value

    async def handle(self, connection):
        owns_connection = False
        try:
            hello = self.decode(await asyncio.wait_for(connection.recv(), 5))
            if hello.get("type") != "hello" or type(hello.get("version")) is not int or hello["version"] != 1:
                raise ProtocolError("expected protocol v1 hello")
            if self.connection is not None:
                await connection.close(1013, "another app is connected")
                return
            self.connection = connection
            owns_connection = True
            self.ready.set()
            async for raw in connection:
                message = self.decode(raw)
                kind = message.get("type")
                if kind == "response":
                    request_id = message.get("id")
                    if not isinstance(request_id, str) or request_id not in self.pending:
                        raise ProtocolError("unknown response id")
                    future = self.pending[request_id]
                    if future.done():
                        raise ProtocolError("duplicate response")
                    if ("result" in message) == ("error" in message):
                        raise ProtocolError("response requires result or error")
                    if "error" in message:
                        error = message["error"]
                        if not isinstance(error, dict) or not isinstance(error.get("code"), (str, int)) or not isinstance(error.get("message"), str):
                            raise ProtocolError("invalid error")
                        future.set_exception(RemoteError(error))
                    else:
                        if not isinstance(message["result"], dict):
                            raise ProtocolError("result must be object")
                        future.set_result(message["result"])
                elif kind == "event":
                    if message.get("event") != "telemetry" or not isinstance(message.get("data"), dict) or any(type(message.get(key)) is not int or message[key] < 0 for key in ("seq", "dropped")):
                        raise ProtocolError("invalid telemetry event")
                    if self.on_event:
                        self.on_event(message)
                else:
                    raise ProtocolError("unexpected message type")
                if self.recorder and not (kind == "response" and message.get("id") in self.unrecorded):
                    self.recorder.write(message)
        except (ProtocolError, asyncio.TimeoutError) as error:
            await connection.close(1008, str(error)[:100] or "hello timed out")
        except ConnectionClosed:
            pass
        finally:
            if owns_connection:
                self.connection = None
                self.ready.clear()
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("app disconnected"))

    async def request(self, method, params=None, timeout=10):
        params = {} if params is None else params
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        if method not in ("ping", "command", "subscribe", "unsubscribe", "screenshot", "ui"):
            raise ValueError("unknown method")
        if method == "command" and (not isinstance(params.get("line"), str) or not 0 < len(params["line"].encode()) <= MAX_COMMAND):
            raise ValueError("command must contain 1..4096 UTF-8 bytes")
        if method == "subscribe" and (type(params.get("hz")) is not int or not 1 <= params["hz"] <= 10):
            raise ValueError("subscription hz must be an integer from 1 to 10")
        if method == "ui" and params.get("action") not in UI_ACTIONS:
            raise ValueError("ui action must be channel, menu, close or status")
        if method == "screenshot":
            action = params.get("action")
            if action not in ("start", "read", "cancel"):
                raise ValueError("screenshot action must be start, read or cancel")
            if action != "start" and (not isinstance(params.get("captureId"), str)
                    or not 0 < len(params["captureId"].encode()) <= 128):
                raise ValueError("captureId must contain 1..128 UTF-8 bytes")
            if action == "read" and (type(params.get("offset")) is not int
                    or not 0 <= params["offset"] <= MAX_CAPTURE_BYTES):
                raise ValueError("capture offset must be a bounded nonnegative integer")
        if len(self.pending) >= MAX_PENDING:
            raise RuntimeError("too many pending requests")
        connection = self.connection
        if connection is None:
            raise ConnectionError("no app connected")
        self.next_id += 1
        request_id = str(self.next_id)
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        if method == "screenshot":
            self.unrecorded.add(request_id)
        try:
            await connection.send(json.dumps({"id": request_id, "method": method, "params": params}))
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)
            self.unrecorded.discard(request_id)

    async def screenshot(self, destination, timeout=DEFAULT_SCREENSHOT_TIMEOUT):
        """Stream one bounded PNG into a private file, publishing only a complete image."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("screenshot timeout must be positive and finite")
        destination = Path(destination)
        capture_id = None
        capture_connection = self.connection
        temporary = None
        try:
            async with asyncio.timeout(timeout):
                started = await self.request("screenshot", {"action": "start"})
                capture_id = started.get("captureId")
                if not isinstance(capture_id, str) or not 0 < len(capture_id.encode()) <= 128:
                    capture_id = None
                    raise ProtocolError("invalid screenshot start response")
                if started.get("state") != "pending":
                    raise ProtocolError("invalid screenshot start state")
                metadata = None
                offset = 0
                header = bytearray()
                with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".screenshot-",
                                                 delete=False) as output:
                    temporary = Path(output.name)
                    os.fchmod(output.fileno(), 0o600)
                    while True:
                        if self.connection is not capture_connection:
                            raise ConnectionError("screenshot session ended")
                        chunk = await self.request("screenshot", {
                            "action": "read", "captureId": capture_id, "offset": offset,
                        })
                        if chunk.get("captureId") != capture_id:
                            raise ProtocolError("screenshot capture ID changed")
                        if chunk.get("state") == "pending":
                            if metadata is not None:
                                raise ProtocolError("screenshot became pending during transfer")
                            await asyncio.sleep(0.05)
                            continue
                        if chunk.get("state") != "ready" or chunk.get("format") != "png":
                            raise ProtocolError("invalid screenshot state or format")
                        current = tuple(chunk.get(key) for key in ("width", "height", "totalBytes"))
                        width, height, total = current
                        if (any(type(value) is not int for value in current)
                                or not 0 < width <= 4096 or not 0 < height <= 4096
                                or not 0 < total <= MAX_CAPTURE_BYTES):
                            raise ProtocolError("invalid screenshot dimensions or size")
                        if metadata is None:
                            metadata = current
                        elif metadata != current:
                            raise ProtocolError("screenshot metadata changed during transfer")
                        if type(chunk.get("offset")) is not int or chunk["offset"] != offset:
                            raise ProtocolError("screenshot offset mismatch")
                        encoded = chunk.get("data")
                        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_CAPTURE_CHUNK + 2) // 3):
                            raise ProtocolError("invalid screenshot chunk size")
                        try:
                            data = base64.b64decode(encoded, validate=True)
                        except (ValueError, binascii.Error) as error:
                            raise ProtocolError("invalid screenshot base64") from error
                        if not 0 < len(data) <= MAX_CAPTURE_CHUNK or offset + len(data) > total:
                            raise ProtocolError("screenshot chunk exceeds declared bounds")
                        if type(chunk.get("done")) is not bool or chunk["done"] != (offset + len(data) == total):
                            raise ProtocolError("invalid screenshot completion marker")
                        header.extend(data[:max(0, 24 - len(header))])
                        output.write(data)
                        offset += len(data)
                        if chunk["done"]:
                            break
                    if (len(header) < 24 or header[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                            or struct.unpack(">II", header[16:24]) != (width, height)):
                        raise ProtocolError("screenshot PNG header disagrees with metadata")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
                temporary = None
                return {"path": str(destination), "width": width, "height": height, "bytes": total}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if capture_id is not None and self.connection is capture_connection:
                with suppress(Exception):
                    await self.request("screenshot", {"action": "cancel", "captureId": capture_id}, timeout=1)


async def stdin_lines():
    """Read bounded lines from a terminal, pipe, or file without blocking asyncio."""
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=1)
    lock = threading.Lock()
    stopped = False
    pending = None
    descriptor = sys.stdin.fileno()

    def read():
        nonlocal pending
        while True:
            try:
                chunk = os.read(descriptor, 4096)
            except OSError as error:
                chunk = error
            with lock:
                if stopped:
                    return
                pending = asyncio.run_coroutine_threadsafe(queue.put(chunk), loop)
                delivery = pending
            try:
                delivery.result()
            except (concurrent.futures.CancelledError, RuntimeError):
                return
            if not chunk or isinstance(chunk, OSError):
                return

    threading.Thread(target=read, daemon=True, name="live-debug-stdin").start()
    buffer = bytearray()
    oversized = False
    try:
        while True:
            chunk = await queue.get()
            if isinstance(chunk, OSError):
                raise chunk
            if not chunk:
                if oversized:
                    yield None
                elif buffer:
                    yield bytes(buffer)
                return
            pieces = chunk.split(b"\n")
            for index, piece in enumerate(pieces):
                if not oversized:
                    buffer.extend(piece)
                    if len(buffer) > 8192:
                        oversized = True
                        buffer.clear()
                if index < len(pieces) - 1:
                    yield None if oversized else bytes(buffer)
                    buffer.clear()
                    oversized = False
    finally:
        with lock:
            stopped = True
            if pending is not None:
                pending.cancel()


async def console(workbench, args):
    await asyncio.wait_for(workbench.ready.wait(), args.wait)
    print("App connected", file=sys.stderr)
    if args.subscribe:
        print(json.dumps(await workbench.request("subscribe", {"hz": args.subscribe})))
    for command in args.command:
        print(json.dumps(await workbench.request("command", {"line": command})))
    if args.ui:
        print(json.dumps(await workbench.request("ui", {"action": args.ui})))
        if args.screenshot:
            await asyncio.sleep(0.25)
    screenshot_timeout = getattr(args, "screenshot_timeout", DEFAULT_SCREENSHOT_TIMEOUT)
    if args.screenshot:
        print(json.dumps(await workbench.screenshot(args.screenshot, timeout=screenshot_timeout)))
    if (args.command or args.ui or args.screenshot) and not args.subscribe:
        return
    print("Enter a game command, /ping, /subscribe HZ, /unsubscribe, /ui ACTION, /screenshot PATH, or /quit", file=sys.stderr)
    reader = stdin_lines()
    try:
        async for raw in reader:
            if raw is None:
                print("Input line too long", file=sys.stderr)
                continue
            try:
                line = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                print("Input must be UTF-8", file=sys.stderr)
                continue
            if not line:
                continue
            if line == "/quit":
                return
            try:
                if line == "/ping":
                    result = await workbench.request("ping")
                elif line.startswith("/subscribe "):
                    result = await workbench.request("subscribe", {"hz": int(line.split()[1])})
                elif line == "/unsubscribe":
                    result = await workbench.request("unsubscribe")
                elif line.startswith("/ui "):
                    words = shlex.split(line)
                    if len(words) != 2:
                        raise ValueError("usage: /ui channel|menu|close|status")
                    result = await workbench.request("ui", {"action": words[1]})
                elif line.startswith("/screenshot "):
                    words = shlex.split(line)
                    if len(words) != 2:
                        raise ValueError("usage: /screenshot PATH")
                    result = await workbench.screenshot(words[1], timeout=screenshot_timeout)
                else:
                    result = await workbench.request("command", {"line": line})
                print(json.dumps(result), flush=True)
            except (ValueError, OSError, ProtocolError, RemoteError, ConnectionError, ConnectionClosed, asyncio.TimeoutError) as error:
                print(str(error) or "request timed out", file=sys.stderr)
    finally:
        await reader.aclose()


async def run(args):
    recorder = Recorder(args.record, args.record_max_bytes) if args.record else None
    workbench = Workbench(args.token, recorder, lambda event: print(json.dumps(event), flush=True))
    try:
        async with await workbench.listen(args.host, args.port):
            print(f"Listening on {args.host}:{args.port}; waiting for app", file=sys.stderr)
            await console(workbench, args)
    finally:
        if recorder:
            recorder.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.environ.get("DUSK_DEBUG_TOKEN"))
    parser.add_argument("--generate-token", action="store_true", help="print a fresh token and exit")
    parser.add_argument("--command", action="append", default=[])
    parser.add_argument("--screenshot", type=Path, help="save one composited PNG through the app debug socket")
    parser.add_argument("--screenshot-timeout", type=float, default=DEFAULT_SCREENSHOT_TIMEOUT,
                        help="total screenshot transfer deadline in seconds (default: 120)")
    parser.add_argument("--ui", choices=UI_ACTIONS, help="inspect or control the app overlay")
    parser.add_argument("--subscribe", type=int, choices=range(1, 11))
    parser.add_argument("--wait", type=float, default=60, help="seconds to wait for app")
    parser.add_argument("--record", help="optional JSONL path; may contain private game state")
    parser.add_argument("--record-max-bytes", type=int, default=16 * 1024 * 1024)
    args = parser.parse_args()
    if args.generate_token:
        print(secrets.token_urlsafe(32))
        return
    if not args.token:
        parser.error("set DUSK_DEBUG_TOKEN or pass --token")
    if not math.isfinite(args.screenshot_timeout) or args.screenshot_timeout <= 0:
        parser.error("screenshot timeout must be positive and finite")
    if args.wait <= 0 or args.record_max_bytes < 0:
        parser.error("wait must be positive and recording limit nonnegative")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, ProtocolError, RemoteError, ConnectionError, ConnectionClosed, asyncio.TimeoutError) as error:
        parser.exit(1, f"live-debug: {error or 'connection timed out'}\n")


if __name__ == "__main__":
    main()
