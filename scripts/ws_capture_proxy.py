#!/usr/bin/env python3
"""Local Responses API WebSocket capture proxy for Codex.

The proxy is intentionally dependency-free. It accepts Codex traffic on a local
OpenAI-compatible `/v1/responses` endpoint, forwards WebSocket frames to the
real upstream endpoint, and writes decoded request/response frames as JSONL.
It also forwards plain HTTP POST requests so Codex can fall back to HTTP/SSE.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import signal
import ssl
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from typing import Optional
from urllib.parse import urlparse


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HEADER_BYTES = 1024 * 1024
READ_CHUNK_SIZE = 64 * 1024
DEFAULT_UPSTREAM_HTTP_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_CAPTURE_ROOT = "captures"
DEFAULT_CAPTURE_SUBDIR = "responses-websocket"
HOP_BY_HOP_HEADERS = {
    "connection",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "sec-websocket-accept",
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
}
REDACT_HEADER_NAMES = {"authorization"}


@dataclass
class HttpRequest:
    method: str
    target: str
    version: str
    headers: list[tuple[str, str]]
    body_prefix: bytes


@dataclass
class WsFrame:
    fin: bool
    opcode: int
    payload: bytes


class CaptureWriter:
    def __init__(self, capture_dir: Path):
        self.capture_dir = capture_dir
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, object] = {}
        self._lock = asyncio.Lock()
        self._seq = 0

    async def write(self, record: dict) -> None:
        session_id = session_id_for_record(record)
        async with self._lock:
            self._seq += 1
            envelope = {
                "schema": "codex.responses_capture.v1",
                "seq": self._seq,
                "wall_time_unix_ms": int(time.time() * 1000),
                "session_id": session_id,
                **record,
            }
            file = self._file_for_session(session_id)
            file.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
            file.write("\n")
            file.flush()

    def _file_for_session(self, session_id: str):
        safe_session_id = safe_path_component(session_id)
        file = self._files.get(safe_session_id)
        if file is None:
            path = self.capture_dir.joinpath(f"{safe_session_id}.jsonl")
            file = path.open("a", encoding="utf-8")
            self._files[safe_session_id] = file
        return file

    def close(self) -> None:
        for file in self._files.values():
            file.close()
        self._files.clear()


def safe_path_component(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return cleaned.strip("._") or "_unknown"


def session_id_for_record(record: dict) -> str:
    value = record.get("session_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "_unknown"


def now_connection_id() -> str:
    return f"conn-{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def header_value(headers: Iterable[tuple[str, str]], name: str) -> Optional[str]:
    needle = name.lower()
    for key, value in headers:
        if key.lower() == needle:
            return value
    return None


def request_session_metadata(req: HttpRequest) -> dict[str, str]:
    metadata = {}
    session_id = header_value(req.headers, "session-id")
    thread_id = header_value(req.headers, "thread-id")
    if session_id:
        metadata["session_id"] = session_id
    if thread_id:
        metadata["thread_id"] = thread_id
    return metadata


def sanitized_headers(headers: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    sanitized = []
    for name, value in headers:
        if name.lower() in REDACT_HEADER_NAMES or "cookie" in name.lower():
            value = "[REDACTED]"
        sanitized.append({"name": name, "value": value})
    return sanitized


def body_as_json_value(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            return {"base64": base64.b64encode(body).decode("ascii")}


def parse_http_headers(raw: bytes) -> HttpRequest:
    head, _, body_prefix = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    if not lines or len(lines[0].split()) != 3:
        raise ValueError("invalid HTTP request line")
    method, target, version = lines[0].split()
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise ValueError(f"invalid HTTP header line: {line!r}")
        name, value = line.split(":", 1)
        headers.append((name.strip(), value.strip()))
    return HttpRequest(method=method, target=target, version=version, headers=headers, body_prefix=body_prefix)


async def read_http_request(reader: asyncio.StreamReader) -> HttpRequest:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            raise EOFError("client closed before request headers")
        data.extend(chunk)
        if len(data) > MAX_HEADER_BYTES:
            raise ValueError("HTTP request headers are too large")
    return parse_http_headers(bytes(data))


async def read_exact_body(
    reader: asyncio.StreamReader,
    body_prefix: bytes,
    content_length: int,
) -> bytes:
    body = bytearray(body_prefix)
    while len(body) < content_length:
        body.extend(await reader.readexactly(content_length - len(body)))
    return bytes(body[:content_length])


def ws_accept_value(client_key: str) -> str:
    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def upstream_target(parsed) -> str:
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return target


async def connect_tcp_or_tls(parsed):
    scheme = parsed.scheme.lower()
    if scheme in {"https", "wss"}:
        port = parsed.port or 443
        context = ssl.create_default_context()
        return await asyncio.open_connection(parsed.hostname, port, ssl=context, server_hostname=parsed.hostname)
    if scheme in {"http", "ws"}:
        port = parsed.port or 80
        return await asyncio.open_connection(parsed.hostname, port)
    raise ValueError(f"unsupported upstream scheme: {parsed.scheme}")


def build_forward_headers(
    incoming: Iterable[tuple[str, str]],
    host: str,
    content_length: Optional[int] = None,
    websocket_key: Optional[str] = None,
) -> list[tuple[str, str]]:
    headers = [("Host", host)]
    if websocket_key is not None:
        headers.extend(
            [
                ("Upgrade", "websocket"),
                ("Connection", "Upgrade"),
                ("Sec-WebSocket-Key", websocket_key),
                ("Sec-WebSocket-Version", "13"),
            ]
        )
    else:
        headers.append(("Connection", "close"))
        if content_length is not None:
            headers.append(("Content-Length", str(content_length)))

    for name, value in incoming:
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS:
            continue
        if websocket_key is None and lower == "content-length":
            continue
        headers.append((name, value))
    return headers


async def read_until_header_end(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            raise EOFError("upstream closed before response headers")
        data.extend(chunk)
        if len(data) > MAX_HEADER_BYTES:
            raise ValueError("upstream response headers are too large")
    head, sep, rest = bytes(data).partition(b"\r\n\r\n")
    return head + sep, rest


def parse_response_status(header_block: bytes) -> tuple[int, list[tuple[str, str]]]:
    lines = header_block.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers.append((name.strip(), value.strip()))
    return status, headers


def frame_opcode_name(opcode: int) -> str:
    return {
        0x0: "continuation",
        0x1: "text",
        0x2: "binary",
        0x8: "close",
        0x9: "ping",
        0xA: "pong",
    }.get(opcode, f"unknown_{opcode}")


async def read_ws_frame(reader: asyncio.StreamReader) -> WsFrame:
    first, second = await reader.readexactly(2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return WsFrame(fin=fin, opcode=opcode, payload=payload)


def encode_ws_frame(frame: WsFrame, mask: bool) -> bytes:
    first = (0x80 if frame.fin else 0) | (frame.opcode & 0x0F)
    payload = frame.payload
    length = len(payload)
    header = bytearray([first])
    mask_bit = 0x80 if mask else 0
    if length < 126:
        header.append(mask_bit | length)
    elif length <= 0xFFFF:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    if not mask:
        return bytes(header) + payload
    key = secrets.token_bytes(4)
    masked_payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + key + masked_payload


def frame_payload_record(frame: WsFrame) -> dict:
    record = {
        "opcode": frame.opcode,
        "opcode_name": frame_opcode_name(frame.opcode),
        "fin": frame.fin,
        "payload_bytes": len(frame.payload),
    }
    if frame.opcode in {0x1, 0x8}:
        try:
            text = frame.payload.decode("utf-8", errors="replace")
            record["payload_text"] = text
            if frame.opcode == 0x1:
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict) and isinstance(parsed.get("type"), str):
                        record["json_type"] = parsed["type"]
                    record["payload_json"] = parsed
                except json.JSONDecodeError:
                    pass
        except Exception:
            record["payload_base64"] = base64.b64encode(frame.payload).decode("ascii")
    elif frame.payload:
        record["payload_base64"] = base64.b64encode(frame.payload).decode("ascii")
    return record


async def handle_http_fallback(
    req: HttpRequest,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    upstream_http_url: str,
    capture: CaptureWriter,
    connection_id: str,
) -> None:
    metadata = request_session_metadata(req)
    content_length_header = header_value(req.headers, "content-length")
    if content_length_header is None:
        await write_simple_response(writer, 411, b"Content-Length is required\n")
        return
    body = await read_exact_body(reader, req.body_prefix, int(content_length_header))
    await capture.write(
        {
            "type": "http_request",
            "connection_id": connection_id,
            **metadata,
            "method": req.method,
            "target": req.target,
            "headers": sanitized_headers(req.headers),
            "body": body_as_json_value(body),
        }
    )

    parsed = urlparse(upstream_http_url)
    upstream_reader, upstream_writer = await connect_tcp_or_tls(parsed)
    host = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
    headers = build_forward_headers(req.headers, host=host, content_length=len(body))
    request_lines = [f"POST {upstream_target(parsed)} HTTP/1.1", *[f"{k}: {v}" for k, v in headers], "", ""]
    upstream_writer.write("\r\n".join(request_lines).encode("iso-8859-1") + body)
    await upstream_writer.drain()

    response_header, response_rest = await read_until_header_end(upstream_reader)
    status, response_headers = parse_response_status(response_header)
    await capture.write(
        {
            "type": "http_response_start",
            "connection_id": connection_id,
            **metadata,
            "status": status,
            "headers": sanitized_headers(response_headers),
        }
    )
    writer.write(response_header)
    if response_rest:
        await capture.write(
            {
                "type": "http_response_chunk",
                "connection_id": connection_id,
                **metadata,
                "payload_text": response_rest.decode("utf-8", errors="replace"),
                "payload_bytes": len(response_rest),
            }
        )
        writer.write(response_rest)
        await writer.drain()

    while True:
        chunk = await upstream_reader.read(READ_CHUNK_SIZE)
        if not chunk:
            break
        await capture.write(
            {
                "type": "http_response_chunk",
                "connection_id": connection_id,
                **metadata,
                "payload_text": chunk.decode("utf-8", errors="replace"),
                "payload_bytes": len(chunk),
            }
        )
        writer.write(chunk)
        await writer.drain()
    upstream_writer.close()
    await upstream_writer.wait_closed()


async def connect_upstream_websocket(req: HttpRequest, upstream_ws_url: str):
    parsed = urlparse(upstream_ws_url)
    upstream_reader, upstream_writer = await connect_tcp_or_tls(parsed)
    websocket_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    host = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
    headers = build_forward_headers(req.headers, host=host, websocket_key=websocket_key)
    request_lines = [f"GET {upstream_target(parsed)} HTTP/1.1", *[f"{k}: {v}" for k, v in headers], "", ""]
    upstream_writer.write("\r\n".join(request_lines).encode("iso-8859-1"))
    await upstream_writer.drain()
    response_header, response_rest = await read_until_header_end(upstream_reader)
    if response_rest:
        raise RuntimeError("unexpected upstream websocket bytes after handshake headers")
    status, response_headers = parse_response_status(response_header)
    if status != 101:
        raise RuntimeError(f"upstream websocket upgrade failed with status {status}")
    return upstream_reader, upstream_writer, response_headers


async def relay_ws_frames(
    source: asyncio.StreamReader,
    sink: asyncio.StreamWriter,
    capture: CaptureWriter,
    connection_id: str,
    metadata: dict[str, str],
    direction: str,
    mask_outbound: bool,
) -> None:
    frame_seq = 0
    while True:
        frame = await read_ws_frame(source)
        frame_seq += 1
        await capture.write(
            {
                "type": "websocket_frame",
                "connection_id": connection_id,
                **metadata,
                "direction": direction,
                "frame_seq": frame_seq,
                **frame_payload_record(frame),
            }
        )
        sink.write(encode_ws_frame(frame, mask=mask_outbound))
        await sink.drain()
        if frame.opcode == 0x8:
            break


async def handle_websocket(
    req: HttpRequest,
    writer: asyncio.StreamWriter,
    reader: asyncio.StreamReader,
    upstream_ws_url: str,
    capture: CaptureWriter,
    connection_id: str,
) -> None:
    metadata = request_session_metadata(req)
    client_key = header_value(req.headers, "sec-websocket-key")
    if not client_key:
        await write_simple_response(writer, 400, b"Missing Sec-WebSocket-Key\n")
        return

    await capture.write(
        {
            "type": "websocket_connect",
            "connection_id": connection_id,
            **metadata,
            "target": req.target,
            "upstream_url": upstream_ws_url,
            "request_headers": sanitized_headers(req.headers),
        }
    )

    upstream_reader, upstream_writer, upstream_headers = await connect_upstream_websocket(req, upstream_ws_url)
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {ws_accept_value(client_key)}\r\n"
        "\r\n"
    )
    writer.write(response.encode("ascii"))
    await writer.drain()
    await capture.write(
        {
            "type": "websocket_connected",
            "connection_id": connection_id,
            **metadata,
            "upstream_response_headers": sanitized_headers(upstream_headers),
        }
    )

    client_to_upstream = asyncio.create_task(
        relay_ws_frames(reader, upstream_writer, capture, connection_id, metadata, "client_to_upstream", mask_outbound=True)
    )
    upstream_to_client = asyncio.create_task(
        relay_ws_frames(upstream_reader, writer, capture, connection_id, metadata, "upstream_to_client", mask_outbound=False)
    )
    done, pending = await asyncio.wait(
        {client_to_upstream, upstream_to_client},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    for task in done:
        exc = task.exception()
        if exc:
            await capture.write({"type": "websocket_relay_error", "connection_id": connection_id, **metadata, "error": str(exc)})
    upstream_writer.close()
    await upstream_writer.wait_closed()


async def write_simple_response(writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
    reason = {
        200: "OK",
        400: "Bad Request",
        403: "Forbidden",
        404: "Not Found",
        411: "Length Required",
        500: "Internal Server Error",
        502: "Bad Gateway",
    }.get(status, "Error")
    response = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body
    writer.write(response)
    await writer.drain()


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    capture: CaptureWriter,
    upstream_ws_url: str,
    upstream_http_url: str,
) -> None:
    connection_id = now_connection_id()
    metadata: dict[str, str] = {}
    try:
        req = await read_http_request(reader)
        metadata = request_session_metadata(req)
        path = req.target.split("?", 1)[0]
        upgrade = (header_value(req.headers, "upgrade") or "").lower()
        if req.method == "GET" and path == "/v1/responses" and upgrade == "websocket":
            await handle_websocket(req, writer, reader, upstream_ws_url, capture, connection_id)
        elif req.method == "POST" and path == "/v1/responses":
            await handle_http_fallback(req, reader, writer, upstream_http_url, capture, connection_id)
        elif req.method == "GET" and path == "/healthz":
            await write_simple_response(writer, 200, b"ok\n")
        else:
            await write_simple_response(writer, 403, b"Only /v1/responses is allowed\n")
    except Exception as exc:
        await capture.write({"type": "connection_error", "connection_id": connection_id, **metadata, "error": str(exc)})
        try:
            await write_simple_response(writer, 502, f"{exc}\n".encode("utf-8", errors="replace"))
        except Exception:
            pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def derive_ws_url(http_url: str) -> str:
    parsed = urlparse(http_url)
    scheme = {"https": "wss", "http": "ws"}.get(parsed.scheme, parsed.scheme)
    return parsed._replace(scheme=scheme).geturl()


async def run(args: argparse.Namespace) -> None:
    capture_dir = resolve_capture_dir(args)
    capture = CaptureWriter(capture_dir)
    upstream_http_url = DEFAULT_UPSTREAM_HTTP_URL
    upstream_ws_url = derive_ws_url(upstream_http_url)
    server = await asyncio.start_server(
        lambda reader, writer: handle_client(reader, writer, capture, upstream_ws_url, upstream_http_url),
        args.host,
        args.port,
    )
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"codex ws capture proxy listening on {sockets}", file=sys.stderr)
    print(f"upstream websocket: {upstream_ws_url}", file=sys.stderr)
    print(f"capture dir: {capture_dir.resolve()}", file=sys.stderr)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    async with server:
        await stop.wait()
    capture.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture and forward Codex Responses WebSocket traffic.")
    parser.add_argument("--host", default="127.0.0.1", help="Local bind host.")
    parser.add_argument("--port", type=int, default=60001, help="Local bind port.")
    parser.add_argument("--capture-dir", default=None, help="Root directory for capture output.")
    parser.add_argument(
        "--capture",
        default=None,
        help="Deprecated compatibility option. If it ends in .jsonl, its stem is used as the capture root.",
    )
    return parser.parse_args(argv)


def resolve_capture_dir(args: argparse.Namespace) -> Path:
    root: Path
    if args.capture_dir:
        root = Path(args.capture_dir)
    elif args.capture:
        path = Path(args.capture)
        if path.suffix == ".jsonl":
            root = path.with_suffix("")
        else:
            root = path
    else:
        root = Path(DEFAULT_CAPTURE_ROOT)
    return root / DEFAULT_CAPTURE_SUBDIR


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
