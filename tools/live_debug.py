#!/usr/bin/env python3
"""Authenticated host workbench for an outgoing Dusklight debug connection."""

import argparse
import asyncio
import hmac
import json
import os
import secrets
import sys
from http import HTTPStatus

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

MAX_MESSAGE = 65536
MAX_COMMAND = 4096
MAX_PENDING = 32


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
                if self.recorder:
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
        if method not in ("ping", "command", "subscribe", "unsubscribe"):
            raise ValueError("unknown method")
        if method == "command" and (not isinstance(params.get("line"), str) or not 0 < len(params["line"].encode()) <= MAX_COMMAND):
            raise ValueError("command must contain 1..4096 UTF-8 bytes")
        if method == "subscribe" and (type(params.get("hz")) is not int or not 1 <= params["hz"] <= 10):
            raise ValueError("subscription hz must be an integer from 1 to 10")
        if len(self.pending) >= MAX_PENDING:
            raise RuntimeError("too many pending requests")
        connection = self.connection
        if connection is None:
            raise ConnectionError("no app connected")
        self.next_id += 1
        request_id = str(self.next_id)
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await connection.send(json.dumps({"id": request_id, "method": method, "params": params}))
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)


async def console(workbench, args):
    await asyncio.wait_for(workbench.ready.wait(), args.wait)
    print("App connected", file=sys.stderr)
    if args.subscribe:
        print(json.dumps(await workbench.request("subscribe", {"hz": args.subscribe})))
    for command in args.command:
        print(json.dumps(await workbench.request("command", {"line": command})))
    if args.command and not args.subscribe:
        return
    print("Enter a game command, /ping, /subscribe HZ, /unsubscribe, or /quit", file=sys.stderr)
    reader = asyncio.StreamReader(limit=8192)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    try:
        while True:
            try:
                raw = await reader.readline()
            except ValueError:
                print("Input line too long", file=sys.stderr)
                continue
            if not raw:
                return
            line = raw.decode("utf-8").strip()
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
                else:
                    result = await workbench.request("command", {"line": line})
                print(json.dumps(result), flush=True)
            except (ValueError, RemoteError, ConnectionError, ConnectionClosed, asyncio.TimeoutError) as error:
                print(str(error) or "request timed out", file=sys.stderr)
    finally:
        transport.close()


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
    if args.wait <= 0 or args.record_max_bytes < 0:
        parser.error("wait must be positive and recording limit nonnegative")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, RemoteError, ConnectionError, ConnectionClosed, asyncio.TimeoutError) as error:
        parser.exit(1, f"live-debug: {error or 'connection timed out'}\n")


if __name__ == "__main__":
    main()
