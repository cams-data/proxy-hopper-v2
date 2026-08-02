# Kubernetes examples

Kubernetes manifests for deploying Proxy Hopper with the Redis backend. These manifests are intended as a starting point — review and adjust resource limits, replica counts, and storage classes for your cluster before applying.

## Files

```
kubernetes/
├── namespace.yaml                  # Isolated namespace for all resources
├── configmap.yaml                  # targets config.yaml
├── secret.yaml                     # Redis URL (base64-encoded)
├── redis.yaml                      # Redis StatefulSet + Service
├── deployment.yaml                 # Proxy Hopper Deployment
├── service.yaml                    # ClusterIP + optional LoadBalancer
├── hpa.yaml                        # HorizontalPodAutoscaler (CPU-based)
├── token-server-deployment.yaml    # Optional: custom token server Deployment
├── token-server-service.yaml       # Optional: token server ClusterIP Service
└── README.md
```

## Prerequisites

- A Kubernetes cluster (1.25+)
- `kubectl` configured against your cluster
- A container image of proxy-hopper published to a registry your cluster can pull from

## Building and pushing the image

The Dockerfile in `examples/docker-compose/local-redis/` installs directly from the
GitHub Release, so no local checkout is needed:

```bash
cd examples/docker-compose/local-redis

docker build \
  --build-arg VERSION=0.1.0 \
  -t your-registry/proxy-hopper:0.1.0 \
  -t your-registry/proxy-hopper:latest \
  .

docker push your-registry/proxy-hopper:0.1.0
docker push your-registry/proxy-hopper:latest
```

Then update the `image:` field in `deployment.yaml` to match.

## Deploying

Apply all manifests in order:

```bash
kubectl apply -f examples/kubernetes/namespace.yaml
kubectl apply -f examples/kubernetes/configmap.yaml
kubectl apply -f examples/kubernetes/secret.yaml
kubectl apply -f examples/kubernetes/redis.yaml
kubectl apply -f examples/kubernetes/deployment.yaml
kubectl apply -f examples/kubernetes/service.yaml
kubectl apply -f examples/kubernetes/hpa.yaml
```

Or apply the whole directory at once:

```bash
kubectl apply -f examples/kubernetes/
```

Check rollout status:

```bash
kubectl rollout status deployment/proxy-hopper -n proxy-hopper
```

## Accessing the proxy

The `service.yaml` exposes two Services:

- **`proxy-hopper`** — `ClusterIP` on port 8080. Use this for in-cluster clients (`http://proxy-hopper.proxy-hopper.svc.cluster.local:8080`).
- **`proxy-hopper-lb`** — `LoadBalancer` on port 8080. Provisions a cloud load balancer for external access. Remove this if you don't need external exposure.

## Updating the target config

The targets config lives in a ConfigMap. Edit it and trigger a rolling restart:

```bash
kubectl edit configmap proxy-hopper-config -n proxy-hopper
kubectl rollout restart deployment/proxy-hopper -n proxy-hopper
```

## Scaling

Manual scale:

```bash
kubectl scale deployment proxy-hopper --replicas=5 -n proxy-hopper
```

The HPA in `hpa.yaml` automatically scales between 2 and 10 replicas based on CPU utilisation. Adjust `minReplicas`, `maxReplicas`, and `averageUtilization` to suit your traffic pattern.

## Admin API and GraphQL

The admin API exposes a management REST interface and a GraphQL API on a separate port (default 8081). It is disabled by default.

To enable it:

1. Add an `auth:` block to `configmap.yaml` (required — the admin API is only useful with auth configured):

   ```yaml
   auth:
     enabled: true
     admin:
       username: admin
       passwordHash: "$2b$12$..."   # proxy-hopper hash-password <password>
     jwtSecret: "change-me-to-a-long-random-string"
   ```

2. Uncomment the `PROXY_HOPPER_ADMIN` env vars and the `admin` containerPort in `deployment.yaml`.

3. Uncomment the `admin` port in `service.yaml`.

4. Rebuild and redeploy:

   ```bash
   kubectl apply -f examples/kubernetes/configmap.yaml
   kubectl apply -f examples/kubernetes/deployment.yaml
   kubectl apply -f examples/kubernetes/service.yaml
   kubectl rollout restart deployment/proxy-hopper -n proxy-hopper
   ```

5. Forward the admin port and explore:

   ```bash
   kubectl port-forward svc/proxy-hopper 8081:8081 -n proxy-hopper

   # Health check (public)
   curl http://localhost:8081/health

   # Obtain a JWT
   curl -X POST http://localhost:8081/auth/login \
        -d "username=admin&password=<password>"

   # GraphQL playground (browser)
   open http://localhost:8081/graphql

   # GraphQL query example
   curl -X POST http://localhost:8081/graphql \
        -H "Authorization: Bearer <jwt>" \
        -H "Content-Type: application/json" \
        -d '{"query":"{ targets { name regex resolvedIps { host port } } }"}'
   ```

Keep the admin port internal — do not expose it through the LoadBalancer Service. Use a `NetworkPolicy` to restrict access to trusted namespaces or pods only.

## Monitoring

If Prometheus is running in your cluster with pod annotation scraping enabled, the metrics port is already annotated in `deployment.yaml`:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "9090"
  prometheus.io/path: "/metrics"
```

## Token server (managed auth)

For targets that require per-request Authorization headers (OAuth tokens, rotating API keys), deploy a custom token server alongside proxy-hopper.

### Deploy

1. **Update `token-server-deployment.yaml`** with your image:

   ```yaml
   image: your-registry/your-token-server:latest
   ```

   Add any environment variables your token server needs under `env:`.

2. **Apply the token server manifests:**

   ```bash
   kubectl apply -f examples/kubernetes/token-server-deployment.yaml
   kubectl apply -f examples/kubernetes/token-server-service.yaml
   ```

3. **Update `configmap.yaml`** — uncomment the `authServer` block and add `authManaged: true` to relevant targets:

   ```yaml
   server:
     authServer:
       url: "http://proxy-hopper-token-server:9000"
       timeoutSeconds: 10
       refreshThresholdSeconds: 60
       retryIntervalSeconds: 30
       maxRetries: 5
       exposeProxyUrl: false

   targets:
     - name: my-api
       regex: 'api\.example\.com'
       authManaged: true
       ipPool: general-pool
       ...
   ```

4. **Apply the updated ConfigMap and restart proxy-hopper:**

   ```bash
   kubectl apply -f examples/kubernetes/configmap.yaml
   kubectl rollout restart deployment/proxy-hopper -n proxy-hopper
   ```

The token server Service is `ClusterIP`-only (`proxy-hopper-token-server:9000`). It is reachable by proxy-hopper in-cluster but not exposed externally.

Your token server must implement:
- `POST /token` — returns `{ headers, expires_at, cursor }`
- `GET /health` — returns `2xx` when ready

See the [managed auth documentation](../../python_modules/proxy-hopper/README.md#managed-auth--token-server) for the full API contract and a minimal Python implementation example.

### Using Helm instead

The Helm chart handles all of this automatically. Set `tokenServer.enabled=true` and provide your image:

```yaml
tokenServer:
  enabled: true
  image:
    repository: your-registry/your-token-server
    tag: "1.0.0"
  port: 9000

config:
  inline: |
    server:
      authServer:
        url: '{{ include "proxy-hopper.tokenServerUrl" . }}'
        ...
    targets:
      - name: my-api
        authManaged: true
        ...
```

See the [Helm chart README](../../charts/proxy-hopper/) for details.

---

## Production hardening checklist

- [ ] Replace the bundled Redis StatefulSet with a managed Redis service (ElastiCache, Memorystore, Redis Cloud) and update `secret.yaml`
- [ ] Set resource `requests` and `limits` in `deployment.yaml` based on observed usage
- [ ] Configure a `PodDisruptionBudget` to maintain minimum availability during node drain
- [ ] Enable Redis AUTH by adding a password to the secret and the Redis config
- [ ] Use a private container registry and configure `imagePullSecrets`
- [ ] Set `PROXY_HOPPER_LOG_FORMAT=json` (already set) and configure your log aggregator
