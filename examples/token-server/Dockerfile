# ── Builder — install dependencies via uv ────────────────────────────────────
FROM python:3.12-slim AS builder

# Pull uv from the official distroless image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy the package manifest first so Docker can cache the dependency layer
# independently of application code changes.
COPY pyproject.toml .

# Install dependencies (not the package itself yet).
# --no-install-project: skip the editable install of the package for now.
# --no-dev: exclude development-only groups if any are defined.
RUN uv sync --no-install-project --no-dev

# Now copy the application source and complete the install.
COPY token_server/ token_server/
RUN uv sync --no-dev

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Copy the uv binary and the installed virtual environment from the builder.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=builder /app /app

WORKDIR /app

EXPOSE 9000

# Runtime configuration — override via docker-compose environment: or -e flags.
ENV TOKEN_SECRET="dev-secret-change-me-in-production" \
    TOKEN_TTL_MINUTES="60" \
    PORT="9000" \
    LOG_LEVEL="INFO"

# Use uv run so the virtualenv created in the builder stage is used directly.
CMD ["uv", "run", "uvicorn", "token_server.main:app", \
     "--host", "0.0.0.0", "--port", "9000", \
     "--log-level", "warning"]
