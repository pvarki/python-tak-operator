#!/usr/bin/env bash
# Validate before retiring the legacy Pod; never remove persistent resources.
set -euo pipefail
context=${1:-kind-rmk8soperator}
namespace=${2:-tak-operator-system}
overlay=${3:-deploy/overlays/local}
kubectl --context "$context" -n "$namespace" apply --dry-run=server -k "$overlay"

# The old selector overlaps the split Deployments, and its config JVM owns the
# shared-file lock. Wait for foreground deletion before creating a new owner.
kubectl --context "$context" -n "$namespace" delete deployment/takserver \
    --ignore-not-found --cascade=foreground --wait=true --timeout=180s
kubectl --context "$context" -n "$namespace" apply -k "$overlay"

# All roles must be created before waiting: config needs messaging's Ignite
# server, while messaging waits for config's shared-file generation marker.
for role in config messaging api; do
    kubectl --context "$context" -n "$namespace" rollout status "deployment/tak-$role" --timeout=600s
done
