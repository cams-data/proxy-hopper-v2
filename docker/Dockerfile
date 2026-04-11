FROM python:3.12-slim

COPY wheels/proxy_hopper-*.whl /tmp/wheels/

RUN pip install --no-cache-dir /tmp/wheels/proxy_hopper-*.whl[uvloop]

VOLUME ["/etc/proxy-hopper"]

EXPOSE 8080

ENTRYPOINT ["proxy-hopper", "run"]
CMD ["--config", "/etc/proxy-hopper/config.yaml"]
