# Sandbox terminal

A real shell in your browser, running in a short-lived, isolated
[Chalk](https://chalk.ai) sandbox. Every visitor gets a fresh sandbox with a gVisor kernel and
no network access. It is deleted when they leave. The terminal is drawn by
[ghostty-web](https://github.com/coder/ghostty-web), Ghostty's VT parser compiled to
WebAssembly.

```
browser (ghostty-web) ──WebSocket──▶ server.py (Chalk ScalingGroup) ──PTY session──▶ fresh sandbox
```

## Files

| File | Role |
| --- | --- |
| `deploy.py` | Builds the server image (with ghostty-web fetched from npm) and deploys it as a `chalkcompute.ScalingGroup`. |
| `server.py` | FastAPI app: `GET /` serves the page, `WS /ws` hands out a sandbox and relays its PTY. |
| `index.html` | The terminal page. Add `?embed=1` to drop the title when you put it in an iframe. |

## Run it

You need [`uv`](https://docs.astral.sh/uv/) and a Chalk environment with sandboxes enabled.
Credentials come from `chalk config` in the current directory. You can also set `CHALK_API_SERVER`,
`CHALK_CLIENT_ID`, `CHALK_CLIENT_SECRET` and `CHALK_ENVIRONMENT_ID` yourself.

```bash
./deploy.py
```

Open the printed `web_url` and press **Launch sandbox**.

The deployed server creates sandboxes with its own workload identity (`chalk_identity=True`), so
no client secret is baked into the image. The identity acts on behalf of whoever deployed it, so
deploy with credentials that can create sandboxes.

## How a visitor's sandbox is locked down

- **Kernel:** `KernelPolicy.RESTRICTED` runs the sandbox under gVisor.
- **Network:** no `network_policy`, which means no egress at all, not even DNS.
- **Credentials:** none. The sandbox gets no workload identity and no secrets.
- **Size:** half a CPU and 1 GiB of memory by default.
- **Lifetime:** every sandbox has a hard `lifetime`, so Chalk deletes it even if the server crashes.
- **Single use:** a sandbox serves exactly one session and is terminated when that session ends.

## Abuse limits

All of these are environment variables on the ScalingGroup.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TERMINAL_MAX_SESSIONS` | `6` | Concurrent sessions. Extra visitors are told to try again. |
| `TERMINAL_SESSION_SECONDS` | `240` | Hard cap on one session. |
| `TERMINAL_IDLE_SECONDS` | `90` | A session with no keystrokes for this long is ended. |
| `TERMINAL_STARTS_PER_MINUTE` | `12` | Sandbox starts across all visitors. |
| `TERMINAL_PER_IP_PER_HOUR` | `10` | Starts per client IP, plus one live session per IP. |
| `TERMINAL_WARM_POOL` | `2` | Started sandboxes kept ready, so a visitor never waits for a cold start. |
| `TERMINAL_POOL_MAX_AGE_SECONDS` | `600` | A pooled sandbox nobody claimed is recycled after this long. |
| `TERMINAL_SANDBOX_CPU` / `TERMINAL_SANDBOX_MEMORY` | `500m` / `1Gi` | Per-sandbox resources. |

Per-IP limits apply only when the server can see the visitor's public address. Behind a gateway
that forwards only its own private address, every visitor would look identical, so the server
falls back to the global limits.

## WebSocket protocol

| Direction | Message |
| --- | --- |
| client → server | `{"type": "input", "data": "<keystrokes>"}` |
| client → server | `{"type": "resize", "cols": 120, "rows": 40}` |
| server → client | binary frames of raw PTY output |
| server → client | `{"type": "status", "message": "..."}` while the sandbox starts |
| server → client | `{"type": "ready", "expires_in": 240}` |
| server → client | `{"type": "busy" \| "limited", "message": "..."}`, then close |
| server → client | `{"type": "ended", "reason": "exited" \| "idle" \| "time_limit" \| "error"}`, then close |

Open the socket at `/ws?cols=<n>&rows=<n>` so the PTY starts at the right size.

## Clean up

```bash
uv run --with chalkcompute python -c \
  "from chalkcompute import ScalingGroup; ScalingGroup.from_name('sandbox-terminal').delete()"
```

Pooled sandboxes expire on their own lifetime within about 15 minutes.

## License

Apache-2.0. ghostty-web is MIT.
