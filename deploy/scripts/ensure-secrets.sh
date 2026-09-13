#!/usr/bin/env bash
# Generate credentials only for the isolated TAK test namespace; preserve them on reruns.
set -euo pipefail
context=kind-rmk8soperator
namespace=tak-operator-system
kubectl --context "$context" apply -f deploy/base/namespace.yaml
if kubectl --context "$context" -n "$namespace" get secret tak-secrets >/dev/null 2>&1; then
    exit 0
fi
credentials=$(mktemp)
trap 'rm -f "$credentials"' EXIT
chmod 600 "$credentials"
for name in POSTGRES_PASSWORD TAKSERVER_CERT_PASS CA_PASS ADMIN_CERT_PASS; do
    printf '%s=%s\n' "$name" "$(openssl rand -hex 24)" >> "$credentials"
done
kubectl --context "$context" -n "$namespace" create secret generic tak-secrets \
    --from-env-file="$credentials"
