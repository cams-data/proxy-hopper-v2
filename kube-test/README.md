# kube-test

Local Kubernetes test environment for Proxy Hopper.

Single replica, memory backend, full metrics. Designed to be deployed to a
local or development cluster (k3s, k3d, kind) with the kube-prometheus-stack
already installed.

## Prerequisites

- `kubectl` configured for your test cluster
- `kustomize` (or `kubectl` >= 1.14 which bundles it)
- Prometheus Operator CRDs (installed with kube-prometheus-stack)
- nginx ingress controller
- Cilium as the LB provider (for Option B in ingress.yaml)

## 1. Build the image

From the **repo root**:

```bash
docker build -f kube-test/Dockerfile -t proxy-hopper:kube-test .
```

For k3d (local registry):
```bash
k3d image import proxy-hopper:kube-test -c <your-cluster-name>
```

For kind:
```bash
kind load docker-image proxy-hopper:kube-test
```

## 2. Configure your proxy IPs

Edit `configmap.yaml` and replace the placeholder IPs in `ipPools[0].ipList`
with your real proxy addresses.

## 3. Deploy

```bash
kubectl apply -k kube-test/
```

This applies all manifests and generates the Grafana dashboard ConfigMap from
the JSON files in `examples/kubernetes/dashboards/`.

## 4. Verify

```bash
kubectl get all -n proxy-hopper-test
kubectl logs -f deployment/proxy-hopper -n proxy-hopper-test
```

Check Prometheus is scraping:
```bash
kubectl port-forward svc/prometheus-operated 9090:9090 -n monitoring
# then open http://localhost:9090/targets and look for proxy-hopper-test
```

## 5. Access

**In-cluster:** `http://proxy-hopper.proxy-hopper-test.svc.cluster.local:8080`

**Via nginx Ingress:** `http://proxy-hopper.test.internal` (after DNS/hosts setup)

**Via Cilium LB (direct):** `http://<EXTERNAL-IP>:8080`
Get the external IP: `kubectl get svc proxy-hopper-lb -n proxy-hopper-test`

## 6. Grafana dashboards

The kustomize configMapGenerator creates a ConfigMap labelled
`grafana_dashboard=1`. The Grafana sidecar picks it up within ~30s.

If Grafana is in a different namespace, configure the sidecar to search this
namespace:
```yaml
grafana:
  sidecar:
    dashboards:
      searchNamespace: proxy-hopper-test
```

## ServiceMonitor note

The ServiceMonitor has a commented-out `release:` label. If your
kube-prometheus-stack was installed with a non-default release name you may
need to add it. Check what your Prometheus is selecting:

```bash
kubectl get prometheus -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}: {.spec.serviceMonitorSelector}{"\n"}{end}'
```

## Tear down

```bash
kubectl delete namespace proxy-hopper-test
```
