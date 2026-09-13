#!/usr/bin/env bash
# Generate credentials only for the isolated TAK test namespace; preserve them on reruns.
set -euo pipefail
context=kind-rmk8soperator
namespace=tak-operator-system
kubectl --context "$context" apply -f deploy/base/namespace.yaml
umask 077
credentials=$(mktemp -d)
trap 'rm -rf "$credentials"' EXIT
if ! kubectl --context "$context" -n "$namespace" get secret tak-secrets >/dev/null 2>&1; then
    for name in TAKSERVER_CERT_PASS CA_PASS ADMIN_CERT_PASS; do
        printf '%s=%s\n' "$name" "$(openssl rand -hex 24)" >> "$credentials/tak.env"
    done
    kubectl --context "$context" -n "$namespace" create secret generic tak-secrets \
        --from-env-file="$credentials/tak.env"
fi
if ! kubectl --context "$context" -n "$namespace" get secret tak-database-app >/dev/null 2>&1; then
    # Retain the old database password when migrating a standalone deployment.
    kubectl --context "$context" -n "$namespace" get secret tak-secrets \
        -o 'go-template={{with index .data "POSTGRES_PASSWORD"}}{{. | base64decode}}{{end}}' \
        > "$credentials/password"
    if [[ ! -s "$credentials/password" ]]; then
        openssl rand -hex 24 | tr -d '\n' > "$credentials/password"
    fi
    kubectl --context "$context" -n "$namespace" create secret generic tak-database-app \
        --type=kubernetes.io/basic-auth --from-literal=username=martiuser \
        --from-file=password="$credentials/password"
fi
