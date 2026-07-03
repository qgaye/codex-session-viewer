# Codex Responses WebSocket Capture Proxy

This repository includes a small local reverse proxy for capturing Codex
Responses API WebSocket traffic without modifying Codex itself.

Important: the proxy is not a transparent interceptor. Codex has no automatic
relationship to port `60001`. Codex only sends traffic to this proxy after you
configure a model provider whose `base_url` points at `http://127.0.0.1:60001/v1`
and start a new Codex session with that provider. Already-running Codex sessions
keep using their existing provider connection and cannot be redirected by this
script.

The proxy:

- listens on `127.0.0.1:60001`
- accepts `GET /v1/responses` WebSocket upgrades from Codex
- forwards frames to `wss://chatgpt.com/backend-api/codex/responses`
- writes decoded WebSocket frames to per-session JSONL files under
  `captures/responses-websocket/`
- forwards `POST /v1/responses` too, so Codex HTTP/SSE fallback still works

## Start Codex With Capture

Run one command from this repository:

```sh
python3 scripts/codex_capture.py
```

This starts the local capture proxy, waits until it is healthy, then launches
Codex with a temporary `ws-capture` provider. When Codex exits, the proxy is
stopped too.

Pass ordinary Codex arguments after `--`:

```sh
python3 scripts/codex_capture.py -- --model gpt-5
```

By default, capture files are written under:

```text
captures/responses-websocket/
```

Use a different port or capture root when needed:

```sh
python3 scripts/codex_capture.py --port 60002 --capture-dir /tmp/codex-captures
```

## Manual Mode

```sh
python3 scripts/ws_capture_proxy.py \
  --port 60001 \
  --capture-dir captures
```

The proxy always uses the normal ChatGPT/Codex login path. `--capture-dir`
names the capture root; actual JSONL files are written under the
`responses-websocket` subdirectory.

In another terminal, start Codex with a temporary local provider:

```sh
codex \
  -c 'model_providers.ws-capture={ name="WS Capture", base_url="http://127.0.0.1:60001/v1", wire_api="responses", requires_openai_auth=true, supports_websockets=true, websocket_connect_timeout_ms=15000 }' \
  -c 'model_provider="ws-capture"'
```

Do not set `env_key`. `requires_openai_auth=true` tells Codex to reuse the auth
it already has, such as the ChatGPT login token stored by Codex. No separate
API key is needed or supported by this capture path.

If you prefer a reusable profile, add a local provider to
`~/.codex/config.toml`:

```toml
[model_providers.ws-capture]
name = "WS Capture"
base_url = "http://127.0.0.1:60001/v1"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = true
websocket_connect_timeout_ms = 15000

[profiles.ws-capture]
model_provider = "ws-capture"
```

Then run Codex with the profile:

```sh
codex -p ws-capture
```

Codex builds the WebSocket URL from `base_url`, so
`http://127.0.0.1:60001/v1` becomes
`ws://127.0.0.1:60001/v1/responses`.

That conversion is the only connection between Codex and the local proxy. If
Codex is launched without this profile, the proxy will keep listening but no
Codex traffic will reach it.

## Capture Format

The proxy reads Codex's `session-id` request header and writes records to:

```text
captures/responses-websocket/<session-id>.jsonl
```

If a request arrives without a session id, it is written to:

```text
captures/responses-websocket/_unknown.jsonl
```

Each line is JSON:

```json
{"schema":"codex.responses_capture.v1","seq":1,"wall_time_unix_ms":0,"session_id":"...","type":"websocket_frame","connection_id":"conn-...","direction":"upstream_to_client","frame_seq":1,"opcode_name":"text","payload_json":{"type":"response.created"}}
```

Important record types:

- `websocket_connect`: local Codex client started a WebSocket connection
- `websocket_connected`: upstream WebSocket upgrade succeeded
- `websocket_frame`: one decoded WebSocket frame
- `http_request`: HTTP/SSE fallback request
- `http_response_start`: HTTP/SSE fallback response headers
- `http_response_chunk`: streamed HTTP/SSE fallback bytes

Authorization and cookie headers are redacted in capture output. Payload bodies
are not redacted because they are the data being inspected.
