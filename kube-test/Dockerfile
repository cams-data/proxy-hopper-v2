FROM python:3.12-slim

# Build from local source — mirrors docker-test/Dockerfile.
# Build context must be the repo root:
#   docker build -f kube-test/Dockerfile -t proxy-hopper:kube-test .
#
# For a local registry (e.g. k3d, kind, or a self-hosted registry):
#   docker build -f kube-test/Dockerfile -t localhost:5000/proxy-hopper:kube-test .
#   docker push localhost:5000/proxy-hopper:kube-test
#
# For Cilium/k3s with a local registry, see README.md.

ARG VERSION=0.0.0.dev0

WORKDIR /build
COPY python_modules/proxy-hopper /build/proxy-hopper

RUN SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION} \
    pip install --no-cache-dir "/build/proxy-hopper[uvloop,metrics]"

VOLUME ["/etc/proxy-hopper"]

EXPOSE 8080 9090

ENTRYPOINT ["proxy-hopper", "run"]
CMD ["--config", "/etc/proxy-hopper/config.yaml"]
