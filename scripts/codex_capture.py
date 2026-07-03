#!/usr/bin/env python3
"""Run Codex with the local WebSocket capture proxy enabled."""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


PROVIDER_CONFIG = (
    'model_providers.ws-capture={{ name="WS Capture", '
    'base_url="http://127.0.0.1:{port}/v1", '
    'wire_api="responses", '
    'requires_openai_auth=true, '
    'supports_websockets=true, '
    'websocket_connect_timeout_ms=15000 }}'
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start the capture proxy, then run Codex through it.",
    )
    parser.add_argument("--port", type=int, default=60001, help="Local capture proxy port.")
    parser.add_argument(
        "--capture-dir",
        default="captures",
        help="Capture root directory. Files are written under responses-websocket/.",
    )
    parser.add_argument("--codex-bin", default="codex", help="Codex executable to run.")
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=5.0,
        help="Seconds to wait for the local proxy to become healthy.",
    )
    parser.add_argument(
        "codex_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed to codex. Prefix with -- when needed.",
    )
    return parser.parse_args(argv)


def wait_for_proxy(port: int, proxy: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/healthz"
    last_error = "proxy did not become healthy"
    while time.monotonic() < deadline:
        exit_code = proxy.poll()
        if exit_code is not None:
            raise RuntimeError(f"capture proxy exited early with status {exit_code}")
        try:
            with urllib.request.urlopen(url, timeout=0.25) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for capture proxy on {url}: {last_error}")


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    codex_args = list(args.codex_args)
    if codex_args[:1] == ["--"]:
        codex_args = codex_args[1:]

    script_dir = Path(__file__).resolve().parent
    proxy_script = script_dir / "ws_capture_proxy.py"
    proxy_cmd = [
        sys.executable,
        str(proxy_script),
        "--port",
        str(args.port),
        "--capture-dir",
        args.capture_dir,
    ]
    codex_cmd = [
        args.codex_bin,
        "-c",
        PROVIDER_CONFIG.format(port=args.port),
        "-c",
        'model_provider="ws-capture"',
        *codex_args,
    ]

    proxy = subprocess.Popen(proxy_cmd)
    codex: subprocess.Popen | None = None
    try:
        wait_for_proxy(args.port, proxy, args.startup_timeout)
        print(
            f"capture proxy ready; writing to {Path(args.capture_dir) / 'responses-websocket'}",
            file=sys.stderr,
        )
        codex = subprocess.Popen(codex_cmd)
        return codex.wait()
    except KeyboardInterrupt:
        if codex is not None and codex.poll() is None:
            codex.send_signal(signal.SIGINT)
            return codex.wait()
        return 130
    finally:
        terminate_process(proxy)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
