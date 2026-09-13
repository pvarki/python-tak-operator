# Local TAK development cluster

Use the existing `kind-rmk8soperator` cluster from the neighboring
`python-rasenmaeher-k8soperator` checkout. `task cluster:up` starts or reuses it;
it does not reset it. The platform operator in that cluster owns the
`platform.opendefence.fi/v1alpha1` CRDs consumed by this operator.

```sh
task tak:render
task tak:up
task tak:status
task tak:logs
task operator:build
task operator:up
task operator:logs
```

Without Task installed, the deployment commands are:

```sh
bash deploy/scripts/ensure-secrets.sh
kubectl --context kind-rmk8soperator apply -k deploy/overlays/local
kubectl --context kind-rmk8soperator -n tak-operator-system \
  rollout status deployment/takserver --timeout=600s
```

All resources live in `tak-operator-system`. The credential helper generates
random database, certificate, CA and administrator passwords into `tak-secrets`
on the first run and preserves them on subsequent runs. Credentials are never
committed. Keep that Secret together with the two PVCs: existing certificates
and database files depend on its contents.

The TAK image is pinned to the exact digest validated in the JNI handoff.
Its standalone initialization script prepares the database schema and certificates.
Configuration, messaging and API run as separate containers in one Pod, sharing
`/opt/tak/data` and a network namespace, matching the integration project's
Compose topology. Retention and plugin services are not needed for file-auth
reconciliation and are omitted from this development deployment. Recreate
deployment strategy prevents two configuration services from writing the same
volume during an update.

The PostGIS image supports the shared cluster's ARM64 node. Local-path PVCs
retain data across Pod restarts. Resource and Ignite cache sizes are deliberately
bounded for the shared development VM.

The operator is a separate Kustomize bundle in `deploy/operator`. Build and
push its production image to the shared **local** registry before applying it:

```sh
docker buildx build --load --target production -t localhost:5005/takoperator:local .
# With Podman (the macOS default when Docker Desktop is not running):
podman push --tls-verify=false localhost:5005/takoperator:local
# With Docker Engine, use: docker push localhost:5005/takoperator:local
kubectl --context kind-rmk8soperator apply -k deploy/operator
kubectl --context kind-rmk8soperator -n tak-operator-system \
  rollout status deployment/tak-operator --timeout=300s
```

After rebuilding an existing `:local` tag, restart only the operator Deployment
to pull the new image. `task operator:up` does this automatically. The operator
runs as a non-root user, stores its JVM runtime files in an `emptyDir`, and
uses a namespace-local `takoperator` Lease for leader election. Its RBAC reads
and patches platform resources, reads local credential Secrets, and updates
the Lease. It has no permission to modify platform status or CRDs.
Recreate strategy also avoids a rollout deadlock: Cloudcoil marks only the
leader ready, so the old Pod must release its Lease before the new Pod can
become ready.
`IGNITE_WORK_DIR` and the process working directory point into that writable
volume. TAK reads the environment variable directly; setting only the Java
system property leaves its default `ignite/work` relative to the process
directory, causing startup to fail on a read-only root filesystem.

The development registry listens on loopback using HTTP. `operator:push` uses
the same runtime selection as the neighboring cluster Taskfile and disables
TLS verification only for this local Podman registry push.

## Ignite connectivity

TAK 5.8.69 accepts `igniteHost` in `TAKIgniteConfig.xml`; it sets the local Ignite
bind address and TCP communication address. The mounted template renders the
TAK Pod IP from the downward API. A headless `takserver` Service exposes that
address directly, so Ignite can discover and advertise its real ports without
Service port translation.

The operator runs in its own Pod. Its bridge writes a separate Ignite config
bound to `TAK_IGNITE_BIND_ADDRESS` (its Pod IP) and uses `TAK_IGNITE_HOST=takserver`
as the discovery seed. The bridge supplies TAKCL's
`com.bbn.marti.takcl.igniteIpAddressOverride` system property so discovery and
local binding are independent. Ignite uses discovery ports 47500–47510 and
communication ports 47100–47110 on the TAK Pod. The operator retains Ignite's
default local port ranges, 47500–47600 and 47100–47200. It is a Java cluster client,
not an HTTP service or Ignite thin client on port 10800.

NetworkPolicies restrict database and Ignite ingress to their consumers. The
default kind networking does not enforce NetworkPolicy; use a policy-capable
CNI when isolation is required. Ignite TLS is disabled for this local cluster.
No Ignite ports are published to the host. `task tak:forward` forwards only
the HTTPS and CoT client ports.

These resources do not install or change the platform CRDs. The neighboring
project's `examples/demo.yaml` contains illustrative public keys, which are not
valid X.509 certificates. Operator reconciliation tests must supply actual test
credentials using the operator's documented credential mechanism.
