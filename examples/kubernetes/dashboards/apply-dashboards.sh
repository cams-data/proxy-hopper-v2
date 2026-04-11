#!/usr/bin/env bash
# apply-dashboards.sh — create/update the Grafana dashboard ConfigMap directly
# without kustomize.
#
# Usage:
#   ./apply-dashboards.sh [namespace]
#
# Default namespace: proxy-hopper
#
# The ConfigMap is labelled grafana_dashboard=1 so the Grafana sidecar picks
# it up automatically.  No Grafana restart required.

set -euo pipefail

NAMESPACE="${1:-proxy-hopper}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

kubectl create configmap proxy-hopper-grafana-dashboards \
  --namespace "${NAMESPACE}" \
  --from-file="${SCRIPT_DIR}/proxy-hopper-admin.json" \
  --from-file="${SCRIPT_DIR}/proxy-hopper-user.json" \
  --dry-run=client -o yaml \
| kubectl label --local -f - grafana_dashboard=1 --dry-run=client -o yaml \
| kubectl apply -f -

echo "Dashboards applied to namespace '${NAMESPACE}'."
echo "The Grafana sidecar will hot-load them within ~30 seconds."
