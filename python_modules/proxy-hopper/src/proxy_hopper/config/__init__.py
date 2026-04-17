"""Configuration loading for Proxy Hopper.

Priority order (highest → lowest):
  1. CLI arguments
  2. YAML config file  (server: block + proxyProviders / ipPools / targets)
  3. Environment variables (PROXY_HOPPER_*)

Target definitions, IP pools, and proxy providers live in the YAML file.
Server-level settings can live in the YAML ``server:`` block, fall back to
``PROXY_HOPPER_*`` env vars (read automatically by ``ServerConfig`` via
``pydantic-settings``), and can always be overridden at the CLI.

Full config file reference
--------------------------
::

    # ---------------------------------------------------------------------------
    # Proxy Providers (optional)
    # ---------------------------------------------------------------------------
    # Named proxy suppliers.  Each provider declares its own credentials and IP
    # list.  Providers are referenced from ipPools via `ipRequests`.
    #
    # Field reference
    # ~~~~~~~~~~~~~~~
    # name          (required) Unique identifier referenced from ipPools.
    # auth          (optional) Credentials block — omit entirely for open or IP-whitelisted proxies.
    #   type        Auth type: basic (default if omitted).
    #   username    Username for HTTP Basic auth.
    #   password    Password for HTTP Basic auth.
    # ipList        (required) List of proxy addresses provided by this supplier.
    #               Accepts "host:port", "host", or "scheme://host:port" forms.
    # regionTag     (optional) Region label attached to metrics — useful for
    #               comparing latency or failure rates across regions/providers.

    proxyProviders:
      - name: provider-au
        auth:
          type: basic
          username: user
          password: secret
        ipList:
          - "proxy-1.example.com:3128"
          - "proxy-2.example.com:3128"
        regionTag: Australia

      - name: provider-ca
        auth:
          type: basic
          username: user
          password: secret
        ipList:
          - "proxy-3.example.com:3128"
          - "proxy-4.example.com:3128"
        regionTag: Canada

    # ---------------------------------------------------------------------------
    # IP Pools (optional)
    # ---------------------------------------------------------------------------
    # Reusable, named IP address lists.  Reference them from targets with
    # `ipPool: <name>`.  IPs can come from providers via `ipRequests` (which
    # selects a random subset from a provider's list) or be listed inline.
    #
    # Field reference
    # ~~~~~~~~~~~~~~~
    # name          (required) Unique identifier referenced from targets.
    # ipRequests    Draw IPs from providers:
    #   provider    Name of a proxyProvider.
    #   count       How many IPs to randomly select from that provider's list.
    # ipList        Inline list of proxy addresses (alternative to ipRequests).
    #
    # ipRequests and ipList can be combined — all selected IPs are merged.

    ipPools:
      - name: pool-1
        ipRequests:
          - provider: provider-au
            count: 5
          - provider: provider-ca
            count: 5

      - name: pool-inline
        ipList:
          - "proxy-5.example.com:3128"

    # ---------------------------------------------------------------------------
    # Targets (required)
    # ---------------------------------------------------------------------------
    # Each target matches incoming request URLs by regex and routes them through
    # the configured proxy IPs.  Targets are evaluated top-to-bottom; the first
    # match wins.
    #
    # IP rotation and rate-limiting
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # The purpose of proxy rotation is to respect per-IP API rate limits.
    # After each request (success or failure) an IP is held off the pool for
    # `minRequestInterval` seconds before being made available again.  If an IP
    # accumulates `ipFailuresUntilQuarantine` consecutive failures it is
    # quarantined for `quarantineTime` seconds, after which it is returned to
    # the pool with its failure counter reset.
    #
    # Field reference
    # ~~~~~~~~~~~~~~~
    # name                      (required) Human-readable label shown in logs/metrics.
    # regex                     (required) Python regex matched against the full request URL.
    # ipPool                    (required*) Name of a shared ipPool.
    # ipList                    (required*) Inline list of proxy addresses.
    #   * Exactly one of ipPool or ipList must be provided.
    # defaultProxyPort          Port applied to IPs listed without an explicit port. [default: 8080]
    # minRequestInterval        How long (seconds / duration string) an IP is held off
    #                           the pool after any request before being reused.
    #                           This is the primary rate-limit knob.             [default: 1s]
    # maxQueueWait              Maximum time (seconds / duration string) a request
    #                           will wait for a free IP before failing.          [default: 30s]
    # numRetries                How many times to retry a failed request using a
    #                           different IP before giving up.                   [default: 3]
    # ipFailuresUntilQuarantine Number of consecutive failures before an IP is
    #                           quarantined.                                     [default: 5]
    # quarantineTime            How long (seconds / duration string) a quarantined
    #                           IP is held out of the pool before being retried. [default: 2m]
    #
    # Duration strings: plain numbers are seconds; append 's', 'm', or 'h' for
    # seconds, minutes, or hours (e.g. "500ms" is not supported — use "0.5s").
    #
    # Identity (optional — disabled by default)
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # Attaches a persistent client persona to each (IP, target) pair so that
    # consecutive requests through the same IP look like the same browser/client
    # to the upstream server.  Each identity carries a fingerprint header bundle
    # (User-Agent, Accept, Accept-Language, Accept-Encoding) and an optional
    # cookie jar that is maintained between requests.  The identity is rotated
    # automatically when the IP is quarantined, on a 429 response, or after a
    # configurable number of requests.
    #
    # identity:
    #   enabled             Master switch.                                       [default: false]
    #   cookies             Persist and replay session cookies per (IP, target). [default: true]
    #   rotateAfterRequests Voluntarily rotate identity after N successful
    #                       requests.  Omit to disable.
    #   rotateOn429         Rotate identity immediately on a 429 response.       [default: true]
    #   warmup              Send a GET to this path through a fresh identity
    #                       before it enters service (collects session cookies).
    #     enabled           [default: true when warmup block is present]
    #     path              URL path for the warmup request.                     [default: /]
    #
    # Note: fingerprint profiles (User-Agent, Accept-* headers) are randomly
    # generated per identity from a rolling version window.  There is no longer
    # a fixed-profile option — each IP gets its own randomly selected persona.

    targets:
      - name: general
        regex: '.*'
        ipPool: pool-1
        minRequestInterval: 2s
        maxQueueWait: 30s
        numRetries: 3
        ipFailuresUntilQuarantine: 5
        quarantineTime: 10m

      - name: strict-api
        regex: 'api[.]example[.]com'
        ipPool: pool-1
        minRequestInterval: 10s
        maxQueueWait: 60s
        numRetries: 1
        ipFailuresUntilQuarantine: 2
        quarantineTime: 30m
        identity:
          enabled: true
          cookies: true
          rotateAfterRequests: 50   # shed sessions before per-session quota is hit
          rotateOn429: true
          warmup:
            enabled: true
            path: /

    # ---------------------------------------------------------------------------
    # Server settings (optional — all have defaults)
    # ---------------------------------------------------------------------------
    # These can also be set via PROXY_HOPPER_* environment variables.
    # CLI flags take the highest priority and override both YAML and env vars.

    server:
      host: 0.0.0.0              # PROXY_HOPPER_HOST
      port: 8080                 # PROXY_HOPPER_PORT
      logLevel: INFO             # PROXY_HOPPER_LOG_LEVEL   (TRACE/DEBUG/INFO/WARNING/ERROR)
      logFormat: text            # PROXY_HOPPER_LOG_FORMAT  (text/json)
      logFile: null              # PROXY_HOPPER_LOG_FILE    (path, or omit for stderr)
      backend: memory            # PROXY_HOPPER_BACKEND     (memory/redis)
      redisUrl: redis://localhost:6379/0  # PROXY_HOPPER_REDIS_URL
      metrics: false             # PROXY_HOPPER_METRICS
      metricsPort: 9090          # PROXY_HOPPER_METRICS_PORT
      debugProbes: false         # PROXY_HOPPER_DEBUG_PROBES     — emit probe DEBUG/TRACE logs (requires logLevel: DEBUG)
      debugQuarantine: false     # PROXY_HOPPER_DEBUG_QUARANTINE — emit quarantine/pool DEBUG/TRACE logs (requires logLevel: DEBUG)
      debugBackend: false        # PROXY_HOPPER_DEBUG_BACKEND    — emit backend storage DEBUG/TRACE logs (requires logLevel: DEBUG)
      probe: true                # PROXY_HOPPER_PROBE
      probeInterval: 60          # PROXY_HOPPER_PROBE_INTERVAL  (seconds)
      probeTimeout: 10           # PROXY_HOPPER_PROBE_TIMEOUT   (seconds)
      probeUrls:                 # PROXY_HOPPER_PROBE_URLS      (comma-separated as env var)
        - http://1.1.1.1
        - http://www.google.com
      admin: false               # PROXY_HOPPER_ADMIN           — enable the admin REST API
      adminPort: 8081            # PROXY_HOPPER_ADMIN_PORT
      adminHost: 0.0.0.0        # PROXY_HOPPER_ADMIN_HOST

    # ---------------------------------------------------------------------------
    # Auth env var overrides (PROXY_HOPPER_AUTH_*)
    # ---------------------------------------------------------------------------
    # Selected auth fields can be injected via environment variables so that
    # secrets never need to appear in a config file or ConfigMap.
    #
    # Auth env vars take precedence over the auth: block in the YAML file.
    # This is the reverse of the server: block (where YAML beats env vars) and
    # is intentional — the typical pattern is to keep non-secret config in YAML
    # and inject secrets from the environment (Kubernetes Secret, Docker secret,
    # CI variable, etc.).
    #
    # PROXY_HOPPER_AUTH_ENABLED          — "true" / "false"
    # PROXY_HOPPER_AUTH_JWT_SECRET       — JWT signing secret
    # PROXY_HOPPER_AUTH_JWT_EXPIRY_MINUTES — token lifetime in minutes
    # PROXY_HOPPER_AUTH_ADMIN_PASSWORD_HASH — bcrypt hash for the admin user
    #                                      (ignored if auth.admin is not set in YAML)
    # PROXY_HOPPER_AUTH_OIDC_ISSUER      — OIDC issuer URL
    # PROXY_HOPPER_AUTH_OIDC_AUDIENCE    — expected 'aud' claim

    # ---------------------------------------------------------------------------
    # Auth (optional)
    # ---------------------------------------------------------------------------
    # Controls who can use the proxy and who can access the admin API.
    # When enabled, every proxy request must supply a valid credential in the
    # ``X-Proxy-Hopper-Auth: Bearer <token>`` header.
    #
    # SECURITY: when auth is enabled, store this block in a Secret (not a plain
    # ConfigMap) so credentials are not readable by unauthorised cluster users.
    # Use ``config.existingSecret`` in the Helm chart or mount a Secret volume.
    #
    # Field reference
    # ~~~~~~~~~~~~~~~
    # enabled           Master switch.  Default: false.
    # jwtSecret         HS256 signing secret for locally-issued tokens.
    #                   Omit (or leave blank) to auto-generate a random secret
    #                   at startup — tokens do not survive restarts in that case.
    # jwtExpiryMinutes  Lifetime of locally-issued tokens.  Default: 60.
    #
    # admin             Local admin user (username/password login via admin API).
    #   username        Login username.
    #   passwordHash    bcrypt hash — generate with: proxy-hopper hash-password <pw>
    #   role            Role assigned on login.  Default: admin.
    #
    # apiKeys           Static Bearer tokens for M2M proxy access.
    #                   API keys can only be used to make proxy requests — they
    #                   have no access to the admin API.
    #   name            Human-readable label shown in logs.
    #   key             The raw key value (sent as the Bearer token).
    #   targets         List of target names this key may access.
    #                   Use ["*"] (default) to allow all targets.
    #
    # oidc              Validate externally-issued JWTs (Authentik, Keycloak, etc.).
    #   issuer          OIDC issuer URL.  JWKS fetched from issuer/.well-known/…
    #   audience        Expected ``aud`` claim.  Leave blank to skip check.
    #   rolesClaim      JWT claim that carries the role name.
    #                   Default: proxy_hopper_role.
    #
    # roles             Custom role definitions (supplement the built-in roles).
    #   Built-in roles: admin (read+write+admin), operator (read+write), viewer (read).
    #   name            Role identifier referenced from apiKeys / admin / OIDC claim.
    #   permissions     List of: read, write, admin.
    #   targets         (optional) Restrict role to named targets only.
    #                   Omit to allow all targets.

    auth:
      enabled: true
      jwtSecret: "change-me-to-a-long-random-string"
      jwtExpiryMinutes: 60

      admin:
        username: admin
        passwordHash: "$2b$12$..."   # proxy-hopper hash-password <password>
        role: admin

      apiKeys:
        - name: my-service
          key: "ph_changeme"
          targets: ["*"]            # ["*"] = all targets (default), or list named targets

      oidc:
        issuer: "https://auth.example.com/application/o/proxy-hopper/"
        audience: "proxy-hopper"
        rolesClaim: proxy_hopper_role

      roles:
        - name: scraper
          permissions: [read, write]
          targets: [general]          # only this target
"""

# Re-export everything callers might import from proxy_hopper.config
from .models import (  # noqa: F401
    AdminUserConfig,
    ApiKeyConfig,
    AuthConfig,
    BasicAuth,
    IdentityConfig,
    IpPool,
    IpRequest,
    OidcConfig,
    Permission,
    ProxyHopperConfig,
    ProxyProvider,
    ResolvedIP,
    RoleConfig,
    ServerConfig,
    TargetConfig,
    WarmupConfig,
    _parse_address,
    _parse_duration,
)
from .normalization import (  # noqa: F401
    _AUTH_ADMIN_CAMEL_TO_SNAKE,
    _AUTH_CAMEL_TO_SNAKE,
    _AUTH_OIDC_CAMEL_TO_SNAKE,
    _DURATION_FIELDS,
    _IDENTITY_CAMEL_TO_SNAKE,
    _IDENTITY_WARMUP_CAMEL_TO_SNAKE,
    _IP_REQUEST_CAMEL_TO_SNAKE,
    _POOL_CAMEL_TO_SNAKE,
    _PROVIDER_CAMEL_TO_SNAKE,
    _SERVER_CAMEL_TO_SNAKE,
    _TARGET_CAMEL_TO_SNAKE,
    _normalise_identity,
    _normalise_pool,
    _normalise_pool_to_model,
    _normalise_provider,
    _normalise_server,
    _normalise_target,
    _parse_auth,
)
from .loader import load_config  # noqa: F401
