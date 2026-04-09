FROM python:3.12-slim

# Install uv for fast dependency installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy package source and install
COPY python_modules/proxy-hopper ./proxy-hopper
RUN uv pip install --system --no-cache ./proxy-hopper

# Config is mounted at runtime — not baked into the image so it can be
# swapped without rebuilding.
VOLUME ["/etc/proxy-hopper"]

EXPOSE 8080

ENTRYPOINT ["proxy-hopper", "run"]
CMD ["--config", "/etc/proxy-hopper/config.yaml"]
