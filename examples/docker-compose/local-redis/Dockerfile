FROM python:3.12-slim

# Install uv for fast dependency installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy both packages — redis backend depends on core
COPY python_modules/proxy-hopper ./proxy-hopper
COPY python_modules/proxy-hopper-redis ./proxy-hopper-redis

# Install core first (local), then redis add-on (also local)
RUN uv pip install --system --no-cache ./proxy-hopper ./proxy-hopper-redis

# Config is mounted at runtime
VOLUME ["/etc/proxy-hopper"]

EXPOSE 8080

ENTRYPOINT ["proxy-hopper", "run"]
CMD ["--config", "/etc/proxy-hopper/config.yaml"]
