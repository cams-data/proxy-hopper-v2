FROM python:3.12-slim

ARG VERSION=0.1.0
ARG GITHUB_REPO=cams-data/proxy-hopper-v2

ARG UVLOOP=true

RUN pip install --no-cache-dir \
    "https://github.com/${GITHUB_REPO}/releases/download/v${VERSION}/proxy_hopper-${VERSION}-py3-none-any.whl" \
 && if [ "$UVLOOP" = "true" ]; then pip install --no-cache-dir uvloop; fi

VOLUME ["/etc/proxy-hopper"]

EXPOSE 8080
# Admin API port — only active when server.admin=true / PROXY_HOPPER_ADMIN=true
EXPOSE 8081

ENTRYPOINT ["proxy-hopper", "run"]
CMD ["--config", "/etc/proxy-hopper/config.yaml"]
