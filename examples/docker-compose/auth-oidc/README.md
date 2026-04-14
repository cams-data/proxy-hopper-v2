# Docker Compose — OIDC authentication

Proxy Hopper deployment that validates Bearer tokens issued by an external OIDC provider. Compatible with [Authentik](https://goauthentik.io/), Keycloak, Dex, Auth0, and any standard OIDC provider.

Two token flows are supported out of the box:

| Flow | Who uses it | Grant type |
|---|---|---|
| Machine-to-machine (M2M) | Services, scripts, CI pipelines | Client credentials |
| Browser / human | Operators accessing via tools or scripts | Authorization code / device flow |

Both flows produce a JWT that Proxy Hopper validates against the provider's JWKS endpoint — no credential sharing, no static secrets passed to clients.

## Files

```
auth-oidc/
├── Dockerfile          # Installs proxy-hopper from a GitHub Release wheel
├── docker-compose.yml  # Single-service Compose definition
├── config.yaml         # OIDC + target config (edit before starting)
└── README.md
```

---

## How it works

1. A client obtains a JWT from your OIDC provider (client credentials, auth code, or device flow).
2. The client sends the JWT with every proxy request: `X-Proxy-Hopper-Auth: Bearer <jwt>`
3. Proxy Hopper fetches the provider's JWKS from `<issuer>/.well-known/openid-configuration`, caches it for 5 minutes, and validates the token signature and expiry.
4. The role embedded in the JWT (via the `rolesClaim` field) determines what the token holder is allowed to do.

---

## Provider setup

### Authentik

#### M2M — service account (client credentials)

1. In the Authentik admin UI, go to **Applications → Providers** and create a new **OAuth2/OpenID Connect Provider**:
   - **Name:** `proxy-hopper`
   - **Client type:** Confidential
   - **Client ID:** `proxy-hopper` (or any value — set `oidc.audience` to match)
   - **Redirect URIs:** not needed for client credentials, enter a placeholder
   - **Scopes:** `openid profile`

2. Create an **Application** that uses this provider.

3. Go to **Directory → Service Accounts** and create a service account for your M2M client. Then go to **Applications → Tokens** and create a token for that account, or use the **OAuth2 Client Credentials** flow directly with the provider's client ID and secret.

4. Set the `proxy_hopper_role` claim on tokens (see [Adding the role claim](#adding-the-role-claim) below).

The issuer URL for Authentik follows the pattern:
```
https://<authentik-host>/application/o/<application-slug>/
```

For example, if your Authentik is at `auth.example.com` and your application slug is `proxy-hopper`:
```yaml
oidc:
  issuer: "https://auth.example.com/application/o/proxy-hopper/"
  audience: "proxy-hopper"
```

#### Browser / human users

Users can obtain a token via the standard OIDC authorization code flow using any OIDC-capable tool or library. The resulting access token is used as the Bearer token for proxy requests.

For command-line use, the [OIDC Device Authorization Grant](https://oauth.net/2/device-flow/) is a good option for interactive login without a browser.

---

### Keycloak

1. Create a **Realm** (or use an existing one).
2. Create a **Client** with:
   - **Client ID:** `proxy-hopper`
   - **Client Protocol:** openid-connect
   - **Access Type:** confidential (for M2M) or public (for browser flows)
3. The issuer URL is `https://<keycloak-host>/realms/<realm-name>`.
4. Add the role claim via a **Protocol Mapper** (see below).

---

### Other providers

Any OIDC-compliant provider works. Set `oidc.issuer` to the provider's issuer URL — Proxy Hopper will auto-discover the JWKS endpoint from `<issuer>/.well-known/openid-configuration`.

---

## Adding the role claim

Proxy Hopper reads the role name from a custom JWT claim (`proxy_hopper_role` by default, configurable via `oidc.rolesClaim`). You need to configure your provider to include this claim in access tokens.

### Authentik — Property Mapping

1. Go to **Customisation → Property Mappings** → Create a **Scope Mapping**:
   - **Name:** `proxy-hopper-role`
   - **Scope name:** `proxy_hopper_role`
   - **Expression:**
     ```python
     # Return the user's proxy-hopper role from their attributes or group membership.
     # Adjust this logic to match how you assign roles in your directory.

     # Option A: from a user attribute named "proxy_hopper_role"
     return request.user.attributes.get("proxy_hopper_role", "viewer")

     # Option B: from group membership
     # if ak_is_group_member(request.user, name="proxy-hopper-admins"):
     #     return "admin"
     # elif ak_is_group_member(request.user, name="proxy-hopper-operators"):
     #     return "operator"
     # return "viewer"
     ```

2. Add this scope mapping to your OAuth2 provider under **Advanced Protocol Settings → Scopes**.

3. Tokens will now include `"proxy_hopper_role": "operator"` (or whichever value the expression returns).

### Keycloak — Protocol Mapper

1. In your Client, go to **Client Scopes → (client)-dedicated → Add Mapper → By Configuration**.
2. Choose **User Attribute**:
   - **Name:** `proxy_hopper_role`
   - **User Attribute:** `proxy_hopper_role`
   - **Token Claim Name:** `proxy_hopper_role`
   - **Claim JSON Type:** String
   - **Add to access token:** On
3. Set the `proxy_hopper_role` attribute on each user in their **Attributes** tab.

---

## Quick start

### 1. Edit `config.yaml`

Set the issuer URL and audience to match your OIDC provider:

```yaml
auth:
  oidc:
    issuer: "https://auth.example.com/application/o/proxy-hopper/"
    audience: "proxy-hopper"
    rolesClaim: proxy_hopper_role
```

Optionally configure a local admin user as a fallback (useful during initial setup):

```bash
proxy-hopper hash-password mysecret
```

Then paste the hash into the `admin.passwordHash` field. Remove the `admin:` block entirely if you want OIDC to be the only auth method.

### 2. Replace the proxy IPs

Edit the `targets` section with your real upstream proxy addresses.

### 3. Build and start

```bash
cd examples/docker-compose/auth-oidc
docker compose up --build
```

---

## Getting a token

### M2M — client credentials (Authentik example)

```bash
TOKEN=$(curl -s -X POST \
  "https://auth.example.com/application/o/token/" \
  -d "grant_type=client_credentials" \
  -d "client_id=proxy-hopper" \
  -d "client_secret=<client-secret>" \
  -d "scope=openid proxy_hopper_role" \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

### Using the token

```bash
# Forwarding mode
curl -H "X-Proxy-Hopper-Auth: Bearer $TOKEN" \
     -H "X-Proxy-Hopper-Target: https://example.com" \
     http://localhost:8080/

# HTTP proxy mode
curl --proxy http://localhost:8080 \
     -H "X-Proxy-Hopper-Auth: Bearer $TOKEN" \
     https://example.com
```

```python
import httpx

# Obtain token via client credentials
resp = httpx.post(
    "https://auth.example.com/application/o/token/",
    data={
        "grant_type": "client_credentials",
        "client_id": "proxy-hopper",
        "client_secret": "<client-secret>",
        "scope": "openid proxy_hopper_role",
    },
)
token = resp.json()["access_token"]

# Use the token for proxy requests
client = httpx.Client(
    headers={"X-Proxy-Hopper-Auth": f"Bearer {token}"},
)
resp = client.get(
    "http://localhost:8080/",
    headers={"X-Proxy-Hopper-Target": "https://example.com"},
)
```

---

## Fallback: local admin login

If you configured the optional `admin:` block in `config.yaml`, you can log in locally via the admin API:

```bash
TOKEN=$(curl -s -X POST http://localhost:8081/auth/login \
  -d "username=admin&password=mysecret" \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -H "X-Proxy-Hopper-Auth: Bearer $TOKEN" \
     -H "X-Proxy-Hopper-Target: https://example.com" \
     http://localhost:8080/
```

---

## Admin API endpoints

| Endpoint | Auth | Description |
|---|---|---|
| `POST /auth/login` | None (credentials in body) | Local login — only available if `admin:` user is configured |
| `GET /health` | None | Liveness check |
| `GET /api/v1/status` | Bearer token (read permission) | Pool and server status |
| `GET /docs` | None | Interactive OpenAPI documentation |

---

## Roles and permissions

Built-in roles:

| Role | Permissions |
|---|---|
| `admin` | read + write + admin |
| `operator` | read + write |
| `viewer` | read only |

Set the role via the `proxy_hopper_role` claim in the JWT. Custom roles with per-target restrictions can be defined in `config.yaml` under `auth.roles`.

---

## Security notes

- **Token expiry is enforced.** Expired tokens are rejected even if the signature is valid.
- **JWKS is cached.** Proxy Hopper caches the provider's public keys for 5 minutes and re-fetches on cache miss or validation failure. No persistent storage is required.
- **The local `jwtSecret` only affects locally-issued tokens** (from `/auth/login`). OIDC tokens are validated with the provider's public key — the `jwtSecret` has no bearing on them.
- **Use TLS in production.** Terminate TLS at a reverse proxy (nginx, Traefik) in front of ports 8080 and 8081.
- **Keep `config.yaml` out of version control.** It contains the `jwtSecret`. Mount it from a Docker Secret or external secrets manager.

---

## Performance — uvloop

By default the Dockerfile installs [uvloop](https://github.com/MagicStack/uvloop), a fast drop-in replacement for asyncio's event loop. Proxy Hopper detects it automatically — no config change needed.

To opt out:

```bash
docker compose build --build-arg UVLOOP=false
```

uvloop is Linux-only and has no effect on Windows or macOS.
