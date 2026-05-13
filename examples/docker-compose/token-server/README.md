# Docker Compose — token server example

Proxy Hopper deployment with a custom token server for managed auth. Use this when your upstream APIs require per-request Authorization headers (OAuth access tokens, rotating session cookies, custom API keys) that must be periodically refreshed.

Proxy-hopper calls your token server before forwarding each request through an `authManaged` target, caches the returned token per proxy IP, and refreshes it automatically before it expires.

## Files

```
token-server/
├── docker-compose.yml   # redis + token-server + proxy-hopper
├── config.yaml          # proxy-hopper config showing authServer + authManaged
└── README.md
```

## How it works

```
client → proxy-hopper → [POST /token → token-server] → upstream API
                          (cached — only called when near expiry)
```

1. A request arrives for an `authManaged` target.
2. proxy-hopper checks its cache for a fresh token for that `(target, proxy-IP)` pair.
3. If no token or near expiry: calls `POST /token` on your token server.
4. The token server returns `{ headers, expires_at, cursor }`.
5. proxy-hopper injects the headers into the upstream request and caches the token.

## Quick start

**1. Build your token server**

Your token server must implement:

```
POST /token  — returns { headers, expires_at, cursor }
GET  /health — returns 2xx when ready
```

See [Token server API contract](#token-server-api-contract) below for the full request/response spec.

**2. Set the image in your environment**

```bash
export TOKEN_SERVER_IMAGE=your-registry/your-token-server:latest
```

Or edit `docker-compose.yml` directly.

**3. Edit `config.yaml`**

Replace the placeholder proxy IPs and adjust the target regex to match your upstream API.

**4. Start**

```bash
cd examples/docker-compose/token-server
docker compose up
```

Proxy-hopper waits for Redis and the token server to pass their healthchecks before starting.

**5. Send a request**

```bash
curl -H "X-Proxy-Hopper-Target: https://api.example.com" \
     http://localhost:8080/v1/endpoint
# proxy-hopper automatically injects the Authorization header from the token server
```

---

## Token server API contract

### `POST /token`

**Request body:**

```json
{
  "target": "my-api",
  "ip":     "203.0.113.10",
  "port":   3128,
  "cursor": {},
  "profile": {
    "user_agent":      "Mozilla/5.0 ...",
    "accept":          "text/html",
    "accept_language": "",
    "accept_encoding": "",
    "extra":           {}
  }
}
```

When `exposeProxyUrl: true` in the proxy-hopper config, the body also includes:

```json
  "proxy_url": "http://proxy-hopper:8080"
```

**Response body (HTTP 200):**

```json
{
  "headers":    { "Authorization": "Bearer eyJ..." },
  "expires_at": "2025-06-01T12:00:00+00:00",
  "cursor":     {}
}
```

| Field | Description |
|---|---|
| `headers` | Injected into the upstream request, replacing any existing headers with the same name |
| `expires_at` | ISO-8601 UTC timestamp. proxy-hopper refreshes the token `refreshThresholdSeconds` before this time. |
| `cursor` | Opaque JSON — returned as-is on the next call for this `(target, ip)`. Use it to carry refresh tokens, session IDs, or sequence numbers between calls. |

On non-200: proxy-hopper increments the failure counter. After `maxRetries` failures the proxy IP is quarantined for auth (`AUTH_BROKEN`) and requests return 502 until the retry window elapses.

### `GET /health`

Return `2xx` when the token server is ready. proxy-hopper calls this at startup before accepting requests.

### Minimal Python example

```python
from datetime import UTC, datetime, timedelta
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class TokenRequest(BaseModel):
    target: str
    ip: str
    port: int
    cursor: dict = {}
    profile: dict = {}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/token")
def get_token(req: TokenRequest):
    # Use req.cursor to carry refresh tokens or session state between calls.
    # On first call, cursor is {}.
    access_token = fetch_or_refresh_token(req.target, req.cursor)
    return {
        "headers":    {"Authorization": f"Bearer {access_token}"},
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "cursor":     {},   # return updated state for next call
    }
```

---

## Configuration reference

Key fields in `config.yaml`:

```yaml
server:
  authServer:
    url: "http://token-server:9000"  # Docker Compose service DNS name
    timeoutSeconds: 10
    refreshThresholdSeconds: 60      # refresh 60s before expiry
    retryIntervalSeconds: 30
    maxRetries: 5
    exposeProxyUrl: false

targets:
  - name: my-api
    authManaged: true                # proxy-hopper injects token headers
    ...
```

See the [main README](../../../python_modules/proxy-hopper/README.md#managed-auth--token-server) for the full config reference and observability details.
