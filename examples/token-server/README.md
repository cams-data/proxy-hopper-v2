# proxy-hopper token server example

End-to-end example showing how to write a custom token server and wire it into proxy-hopper. The token server runs as a separate service; proxy-hopper calls it before every forwarded request and injects the returned Authorization headers automatically.

## What's in this directory

```
token-server/
├── token_server/        # Python package — the token server implementation
│   ├── __init__.py
│   └── main.py          # FastAPI app: POST /token + GET /health
├── pyproject.toml       # uv-managed project (uv init --app)
├── .python-version      # Python 3.12
├── Dockerfile           # builds the token server image (multi-stage, uv)
├── docker-compose.yml   # token-server + proxy-hopper
├── config.yaml          # proxy-hopper config: authServer + authManaged target
└── README.md
```

## How it works

```
client
  │  curl -H "X-Proxy-Hopper-Target: https://httpbin.org" http://localhost:8080/get
  ▼
proxy-hopper
  │  target "httpbin" matched, authManaged=true
  │  token cached? → use it
  │  token missing / near expiry?
  ▼
token-server  POST /token  →  { headers, expires_at, cursor }
  │                              ↑ Authorization: Bearer eyJ...
  │
  ▼  inject Authorization header into upstream request
external proxy  →  httpbin.org/get
  │                  ↑ echoes all request headers back as JSON
  ▼
response returned to client (Authorization header visible in response body)
```

The token server generates signed JWTs. In production you replace the JWT generation with your real auth mechanism — OAuth2, session cookies, rotating API keys. The Compose wiring and proxy-hopper config stay identical.

## Quick start

### 1. Prerequisites

- Docker and Docker Compose
- At least one real external HTTP proxy IP (see `config.yaml`)

### 2. Add your proxy IPs

Edit `config.yaml` and replace the placeholder IPs in `proxyProviders` with real external proxy addresses — both targets (`httpbin` and `general`) draw from the same pool:

```yaml
proxyProviders:
  - name: general-provider
    ipList:
      - "your-proxy-1.example.com:3128"   # replace with a real proxy IP
      - "your-proxy-2.example.com:3128"
```

### 3. (Optional) Set a production secret

```bash
export TOKEN_SECRET="$(openssl rand -hex 32)"
```

If you skip this, the server uses `dev-secret-change-me-in-production`.

### 4. Start

```bash
cd examples/token-server
docker compose up --build
```

Docker builds the token server image from the local `Dockerfile`, pulls the proxy-hopper image from `ghcr.io`, and starts both services. proxy-hopper waits for the token server to pass its healthcheck before accepting connections.

### 5. Test it

**Check the token server directly:**

```bash
# Health check
curl http://localhost:9000/health
# {"status":"ok"}

# Manually call /token (same payload proxy-hopper sends)
curl -s -X POST http://localhost:9000/token \
  -H "Content-Type: application/json" \
  -d '{
    "target": "httpbin",
    "ip": "203.0.113.10",
    "port": 3128,
    "cursor": {}
  }' | python -m json.tool
# {
#   "headers": { "Authorization": "Bearer eyJ..." },
#   "expires_at": "2025-06-01T13:00:00+00:00",
#   "cursor": { "refresh_count": 1, "last_issued": "..." }
# }
```

**Send a proxied request through proxy-hopper:**

```bash
# Forwarding mode — proxy-hopper injects the Authorization header before
# forwarding to httpbin.org.  The response echoes all request headers back.
curl -s \
  -H "X-Proxy-Hopper-Target: https://httpbin.org" \
  http://localhost:8080/get | python -m json.tool

# Look for the injected header in the response:
# {
#   "headers": {
#     "Authorization": "Bearer eyJ...",   <-- injected by proxy-hopper ✓
#     "Host": "httpbin.org",
#     ...
#   }
# }
```

```python
import requests

session = requests.Session()
session.headers["X-Proxy-Hopper-Target"] = "https://httpbin.org"

resp = session.get("http://localhost:8080/get")
print(resp.json()["headers"]["Authorization"])
# Bearer eyJ...
```

**Watch the cursor in action** — call twice and observe `refresh_count` incrementing:

```bash
# First call — cursor is {}
curl -s -X POST http://localhost:9000/token \
  -H "Content-Type: application/json" \
  -d '{"target":"httpbin","ip":"1.2.3.4","port":3128,"cursor":{}}'
# → "cursor": { "refresh_count": 1, "last_issued": "..." }

# Second call — pass the cursor back (proxy-hopper does this automatically)
curl -s -X POST http://localhost:9000/token \
  -H "Content-Type: application/json" \
  -d '{"target":"httpbin","ip":"1.2.3.4","port":3128,"cursor":{"refresh_count":1,"last_issued":"..."}}'
# → "cursor": { "refresh_count": 2, ... }
```

---

## Adapting to your auth mechanism

Open `token_server/main.py`. The only function you need to change is `_issue_token()`:

```python
def _issue_token(target, ip, port, expires_at, cursor):
    # Replace this with your real auth call.
    ...
    return token_string
```

### OAuth2 client credentials

```python
import httpx

async def _issue_token(target, ip, port, expires_at, cursor):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://auth.example.com/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": os.environ["OAUTH_CLIENT_ID"],
                "client_secret": os.environ["OAUTH_CLIENT_SECRET"],
                "scope": f"proxy:{target}",
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
```

### Refresh token exchange (carry the refresh token in the cursor)

```python
async def _issue_token(target, ip, port, expires_at, cursor):
    refresh_token = cursor.get("refresh_token")
    async with httpx.AsyncClient() as client:
        if refresh_token:
            resp = await client.post(
                "https://auth.example.com/oauth/token",
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            )
        else:
            resp = await client.post(
                "https://auth.example.com/oauth/token",
                data={"grant_type": "client_credentials", ...},
            )
        data = resp.json()
        # Store the new refresh token in the cursor for the next call.
        cursor["refresh_token"] = data["refresh_token"]
        return data["access_token"]
```

Note: because `cursor` is a dict shared by reference in `get_token()`, updating it in `_issue_token()` is reflected in `new_cursor` automatically.

### Session-cookie auth

If the API requires a session cookie instead of a Bearer token:

```python
async def _issue_token(target, ip, port, expires_at, cursor):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.example.com/login",
            json={"username": os.environ["API_USER"], "password": os.environ["API_PASS"]},
        )
        session_cookie = resp.cookies["session"]
        return session_cookie  # caller wraps this in {"Cookie": f"session={token}"}
```

And update `get_token()` to return `"Cookie"` instead of `"Authorization"`:

```python
return {
    "headers": {"Cookie": f"session={token}"},
    ...
}
```

---

## Development

```bash
cd examples/token-server

# Install dependencies (creates .venv/)
uv sync

# Run locally (without Docker)
uv run uvicorn token_server.main:app --reload --port 9000

# Run tests (add tests/ if you extend this)
uv run pytest
```

To generate a lockfile for reproducible builds:

```bash
uv lock
# then add to Dockerfile: COPY uv.lock .
# and change: RUN uv sync --frozen --no-dev
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TOKEN_SECRET` | `dev-secret-change-me-in-production` | HMAC key for signing JWTs. **Change this in production.** |
| `TOKEN_TTL_MINUTES` | `60` | Token lifetime in minutes. Must be longer than `refreshThresholdSeconds` in `config.yaml`. |
| `LOG_LEVEL` | `INFO` | Logging verbosity: `DEBUG` \| `INFO` \| `WARNING`. |

---

## Production checklist

- [ ] Set `TOKEN_SECRET` to a strong random value (`openssl rand -hex 32`)
- [ ] Replace `_issue_token()` in `main.py` with your real auth mechanism
- [ ] Add `uv lock` and use `--frozen` in the Dockerfile for reproducible builds
- [ ] Switch proxy-hopper to the Redis backend (`PROXY_HOPPER_BACKEND=redis`) when running multiple replicas — token cache is then shared across instances
- [ ] Set `server.authServer.exposeProxyUrl: true` if your token server needs the proxy URL for callbacks or auditing
- [ ] Monitor `proxy_hopper_auth_broken_ips_current` — a non-zero value means the token server is returning errors for some IPs
