# Docker Compose — API key authentication

Proxy Hopper deployment with static API key authentication and a local admin user. Suitable for teams that want to lock down proxy access without operating an external identity provider.

Every proxy request must present a valid Bearer token. Tokens are either static API keys (long-lived, defined in `config.yaml`) or short-lived JWTs issued by the admin API on login.

## Files

```
auth-api-keys/
├── Dockerfile          # Installs proxy-hopper from a GitHub Release wheel
├── docker-compose.yml  # Single-service Compose definition
├── config.yaml         # Auth + target config (edit before starting)
└── README.md
```

## Quick start

### 1. Hash your admin password

Run the `hash-password` helper — it outputs a bcrypt hash to paste into `config.yaml`:

```bash
# Using the image before it's fully started (no deps needed locally)
docker compose run --rm proxy-hopper hash-password mysecret

# Or if proxy-hopper is installed in your local Python environment
proxy-hopper hash-password mysecret
```

Copy the output (starts with `$2b$12$…`) into `config.yaml`:

```yaml
auth:
  admin:
    username: admin
    passwordHash: "$2b$12$<paste-output-here>"
```

### 2. Set your API keys

Replace the placeholder key values in `config.yaml` with long random strings. Keep them out of version control — use a `.env` file or a secrets manager.

```yaml
auth:
  apiKeys:
    - name: my-scraper
      key: "ph_scraper_CHANGEME_use_a_long_random_value"
      targets: ["*"]          # allow all targets
    - name: restricted-pipeline
      key: "ph_pipeline_CHANGEME_use_a_long_random_value"
      targets: [general]      # restrict to the "general" target only
```

You can generate a suitable key with:

```bash
python -c "import secrets; print('ph_' + secrets.token_urlsafe(32))"
```

### 3. Replace the proxy IPs

Edit the `targets` section with your real upstream proxy addresses.

### 4. Build and start

```bash
cd examples/docker-compose/auth-api-keys
docker compose up --build
```

---

## Sending authenticated requests

### Using a static API key

Pass the key as a Bearer token in the `X-Proxy-Hopper-Auth` header:

```bash
# Forwarding mode
curl -H "X-Proxy-Hopper-Auth: Bearer ph_scraper_CHANGEME_use_a_long_random_value" \
     -H "X-Proxy-Hopper-Target: https://example.com" \
     http://localhost:8080/

# HTTP proxy mode
curl --proxy http://localhost:8080 \
     -H "X-Proxy-Hopper-Auth: Bearer ph_scraper_CHANGEME_use_a_long_random_value" \
     https://example.com
```

```python
import httpx

KEY = "ph_scraper_CHANGEME_use_a_long_random_value"

# Forwarding mode
resp = httpx.get(
    "http://localhost:8080/",
    headers={
        "X-Proxy-Hopper-Auth": f"Bearer {KEY}",
        "X-Proxy-Hopper-Target": "https://example.com",
    },
)

# HTTP proxy mode
resp = httpx.get(
    "https://example.com",
    proxy="http://localhost:8080",
    headers={"X-Proxy-Hopper-Auth": f"Bearer {KEY}"},
)
```

### Using the admin API (login → JWT)

Log in to get a short-lived JWT, then use it exactly like an API key:

```bash
# 1. Login — returns a JSON body with access_token
TOKEN=$(curl -s -X POST http://localhost:8081/auth/login \
  -d "username=admin&password=mysecret" \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# 2. Use the token
curl -H "X-Proxy-Hopper-Auth: Bearer $TOKEN" \
     -H "X-Proxy-Hopper-Target: https://example.com" \
     http://localhost:8080/
```

```python
import httpx

# 1. Login
resp = httpx.post(
    "http://localhost:8081/auth/login",
    data={"username": "admin", "password": "mysecret"},
)
token = resp.json()["access_token"]

# 2. Use the token for proxy requests
resp = httpx.get(
    "http://localhost:8080/",
    headers={
        "X-Proxy-Hopper-Auth": f"Bearer {token}",
        "X-Proxy-Hopper-Target": "https://example.com",
    },
)
```

---

## Admin API endpoints

The admin API runs on port 8081.

| Endpoint | Auth | Description |
|---|---|---|
| `POST /auth/login` | None (credentials in body) | Exchange username/password for a JWT |
| `GET /health` | None | Liveness check — always returns `{"status": "ok"}` |
| `GET /api/v1/status` | Bearer token (read permission) | Pool and server status |
| `GET /docs` | None | Interactive OpenAPI documentation |

```bash
# Health check
curl http://localhost:8081/health

# Status (requires a valid token)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8081/api/v1/status
```

---

## Target access control

API keys do not use roles. Each key carries a `targets` list that controls which proxy targets it may route requests to.

```yaml
auth:
  apiKeys:
    - name: unrestricted
      key: "ph_abc..."
      targets: ["*"]          # wildcard — allow all targets (default)

    - name: restricted
      key: "ph_xyz..."
      targets: [general]      # only the target named "general"

    - name: multi-target
      key: "ph_def..."
      targets: [general, strict-api]
```

API keys **cannot** access the admin API (port 8081) — that requires a locally-issued JWT from `/auth/login` or an OIDC token. The admin API uses roles (`admin`/`operator`/`viewer`) for its own access control.

---

## Security notes

- **Keep `config.yaml` out of version control.** It contains the `jwtSecret` and API key values. Use Docker Secrets, a secrets manager, or mount it from a volume that is not committed.
- **Set a strong `jwtSecret`.** If omitted, one is auto-generated at startup and tokens do not survive a restart.
- **Rotate API keys** by updating `config.yaml` and restarting the container (`docker compose restart proxy-hopper`).
- **Use TLS in production.** Put an nginx or Traefik reverse proxy in front of both ports 8080 and 8081 and terminate TLS there.

---

## Performance — uvloop

By default the Dockerfile installs [uvloop](https://github.com/MagicStack/uvloop), a fast drop-in replacement for asyncio's event loop. Proxy Hopper detects it automatically — no config change needed.

To opt out:

```bash
docker compose build --build-arg UVLOOP=false
```

uvloop is Linux-only and has no effect on Windows or macOS.
