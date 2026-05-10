FROM python:3.12-slim

COPY wheels/proxy_hopper-*.whl wheels/proxy_hopper_webserver-*.whl /tmp/wheels/

RUN WHEEL=$(echo /tmp/wheels/proxy_hopper-*.whl) && \
    WS_WHEEL=$(echo /tmp/wheels/proxy_hopper_webserver-*.whl) && \
    pip install --no-cache-dir "${WHEEL}[uvloop]" "${WS_WHEEL}"

VOLUME ["/etc/proxy-hopper"]

EXPOSE 8080
# Admin API port — only active when server.admin=true / PROXY_HOPPER_ADMIN=true
EXPOSE 8081

ENTRYPOINT ["proxy-hopper"]
CMD ["run", "--config", "/etc/proxy-hopper/config.yaml"]
