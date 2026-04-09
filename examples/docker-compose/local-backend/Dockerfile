FROM python:3.12-slim

ARG VERSION=0.1.0
ARG GITHUB_REPO=cams-data/proxy-hopper-v2

RUN pip install --no-cache-dir \
    "https://github.com/${GITHUB_REPO}/releases/download/v${VERSION}/proxy_hopper-${VERSION}-py3-none-any.whl"

VOLUME ["/etc/proxy-hopper"]

EXPOSE 8080

ENTRYPOINT ["proxy-hopper", "run"]
CMD ["--config", "/etc/proxy-hopper/config.yaml"]
