# Local TAK development cluster

With Task and the neighboring `python-rasenmaeher-k8soperator` checkout available,
run `task up` to start the complete environment. It ensures
the sibling's `up` workflow is ready, then runs `tak:up` and `operator:up` in order.
The shared cluster is `kind-rmk8soperator`; the sibling platform operator owns the
`platform.opendefence.fi/v1alpha1` CRDs consumed here.

`up` and `platform:up` automatically resolve the sibling's tools through its
`mise.toml` when mise is installed. This also works with unconfigured mise shims
on PATH; global tool versions and prior shell activation are unnecessary.
Without mise, the required tools must already be on PATH.

`task platform:up` reuses a Tilt session belonging to that sibling checkout, or
starts the sibling's foreground `task up` in the background and logs to
`.task/platform-up.log`. It waits for the platform's Tilt readiness, established
CRDs, and available Deployment. A session from another checkout on the selected
port is rejected. `TILT_PORT` defaults to 10350 and `PLATFORM_START_TIMEOUT` to
600 seconds. Use the sibling's `task tilt:stop` to stop that background session;
its cluster lifecycle commands remain authoritative. Demo resources still use
their manual Tilt trigger.

Individual tasks remain available:

```sh
task platform:up
task cnpg:up
task tak:render
task tak:up
task tak:status
task tak:logs
task operator:up
task operator:logs
```

For CNPG installation, database readiness, and commands without Task, see
[database setup](database/README.md). Once CNPG and its database are ready:

```sh
bash deploy/scripts/deploy-tak.sh kind-rmk8soperator tak-operator-system
```

TAK and its CNPG Cluster live in `tak-operator-system`; the CNPG operator runs in
`cnpg-system`. The credential helper generates database credentials in
`tak-database-app` and certificate, CA and administrator passwords in `tak-secrets`,
preserving existing values on subsequent runs. Credentials are never committed.
Keep both Secrets together with the TAK and CNPG PVCs: certificates and database
files depend on their contents.

The TAK image and operator JAR source pin the same multi-architecture PR #136
digest. Configuration, messaging and API each run in a separate Deployment and
Pod, using the image's native startup support. Config's init container alone
initializes the database schema and certificates. All roles start together:
config needs messaging's Ignite server, while messaging waits for config's
shared-file generation marker. Startup and readiness probes run the image's
profile-specific health check inside each Pod. Retention and plugin manager
remain omitted from this development deployment.

`tak:up` server-validates the overlay before retiring the legacy `takserver`
Deployment with foreground deletion. This waits for its config JVM to release
the shared-file lock and prevents overlapping Deployment selectors. It then
applies all three roles before waiting for their rollouts. Subsequent runs reuse
the split Deployments. Use this task or its helper for the first migration;
plain `kubectl apply` would leave the old Deployment running. Recreate strategy
keeps each role at one instance through updates.

Use `task tak:restart` when restarting the Ignite grid. It stops all TAK
Pods before bringing the roles up together, waits for readiness, and reconnects
the operator. This prevents a new client from attaching to the retiring grid. During local
testing, replacing messaging alone left the existing config/API Ignite clients
stalled even though their listeners remained open. The image's health check
checks local listeners and shared-file ownership, so it does not detect that
JVM reconnect failure. Separate Pods do not yet provide transparent messaging
failover; independent messaging replacement requires this coordinated recovery.
See [split-Pod validation](../references/TAK_SPLIT_POD_VALIDATION.md) for the live
checks and the observed Ignite reconnect failure.

The roles still share `/opt/tak/data` for certificates, CoreConfig and file-auth
state. Required Pod affinity places messaging and API on the config Pod's node,
allowing them to share the existing ReadWriteOnce PVC. This is process isolation,
not multi-node availability: production placement across nodes needs shared
storage with verified file-lock semantics or a change to state distribution.
Each Pod has its own runtime `emptyDir` for Ignite XML, work files and outbound
Java truststore. Preserve `tak-data`, the CNPG PVCs and both credential Secrets
during migration. The [original review](../references/TAK_SIDECAR_REMOVAL_REVIEW.md)
records the upstream constraints behind this layout.

CNPG manages the PostgreSQL Pods, storage and primary Service `tak-database-rw`.
The pinned PostGIS image supports the shared cluster's ARM64 node. CNPG initializes
the database and extensions; TAK's SchemaManager creates the application schema
using a non-superuser owner account. The local overlay runs one database instance
with a 2 GiB PVC; `deploy/database/base` defines three instances with 10 GiB each.
Local-path PVCs retain data across Pod restarts. Resource and Ignite cache sizes
are bounded for the shared development VM. Follow the
[migration instructions](database/README.md#migrating-the-previous-local-database)
before using these tasks with the previous standalone database Deployment.

The operator is a separate Kustomize bundle in `deploy/operator`.
`task operator:up` ensures a local image exists, pushes it to the shared **local**
registry, then applies and restarts the Deployment. It builds only when the local
image is missing. This sequence also works when the registry has been recreated
and its previous images are gone. After changing code or the Dockerfile, use
`task operator:build` to rebuild and publish, then `task operator:up` to deploy.

To rebuild and deploy without Task:

```sh
docker buildx build --load --target production -t localhost:5005/takoperator:local .
# With Podman (the macOS default when Docker Desktop is not running):
podman push --tls-verify=false localhost:5005/takoperator:local
# With Docker Engine, use: docker push localhost:5005/takoperator:local
kubectl --context kind-rmk8soperator apply -k deploy/operator
kubectl --context kind-rmk8soperator -n tak-operator-system \
  rollout restart deployment/tak-operator
kubectl --context kind-rmk8soperator -n tak-operator-system \
  rollout status deployment/tak-operator --timeout=300s
```

Restarting the operator Deployment makes it pull the updated `:local` tag. A
build or push failure stops `operator:up` before it changes the Deployment. The operator
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

Each TAK Pod sets `TAK_IGNITE_BIND_ADDRESS` from its own downward-API Pod IP.
The image renders `igniteHost` in private XML, with `TAK_IGNITE_SEEDS=tak-ignite:47500`.
The messaging-only headless `tak-ignite` Service uses `publishNotReadyAddresses`
so bootstrap does not depend on an already-ready Ignite grid. Config and API
are Ignite clients; messaging is its sole server. The old mounted Ignite
template is removed. Keep the new server renderer's default finder: it uses
`igniteMulticast=true` to retain the explicit discovery seeds, whereas TAK's
server-side unicast finder overwrites them with the local bind address.

The operator runs in its own Pod. Its bridge writes a separate Ignite config
bound to `TAK_IGNITE_BIND_ADDRESS` (its Pod IP) and uses `TAK_IGNITE_HOST=tak-ignite`
as the discovery seed. The bridge supplies TAKCL's
`com.bbn.marti.takcl.igniteIpAddressOverride` system property so discovery and
local binding are independent. Its TAKCL XML explicitly keeps `igniteMulticast=false`;
the server XML must not be copied into this client. Messaging listens on discovery
port 47500; each TAK Pod uses communication port 47100. The operator retains Ignite's
default local port ranges, 47500–47600 and 47100–47200. It is a Java cluster client,
not an HTTP service or Ignite thin client on port 10800.

NetworkPolicies restrict database and Ignite ingress to their consumers. The
default kind networking does not enforce NetworkPolicy; use a policy-capable
CNI when isolation is required. Ignite TLS is disabled for this local cluster.
No Ignite ports are published to the host. `takserver` remains the API's HTTPS
Service, preserving the existing certificate hostname. `tak-messaging` serves
CoT. `task tak:forward` runs both Service forwards concurrently; the
`tak:forward:https` and `tak:forward:cot` tasks run them individually.

These resources do not install or change the platform CRDs. The neighboring
project's `examples/demo.yaml` omits TAK credentials. Operator reconciliation
tests must supply actual test credentials using the operator's documented
credential Secret mechanism; the legacy `spec.publicKey` field is optional.
