# TAK PR #136 migration review

Historical review before implementation. The process split is now implemented;
see [current deployment instructions](../deploy/README.md) for the active layout.
The [split-Pod validation](TAK_SPLIT_POD_VALIDATION.md) records tested behavior
and the messaging-replacement limitation found during implementation.
The findings below describe the original shared-Pod baseline and its migration
requirements, including the remaining production storage/CNI validation.

Reviewed 2026-09-13 against [docker-atak-server PR #136](https://github.com/pvarki/docker-atak-server/pull/136),
head `025355ba9f76bf6c41187897f4a5fcac93b82afb`, and this repository at `2d267fe`.
The PR was open, with its checks, Compose validation and publishing jobs passing.

**The Python operator already runs in its own Pod. TAK's configuration,
messaging and API services still share one Pod here.** PR #136 supplies the
server startup support to separate them, but the Kubernetes migration remains.
Changing the image tag alone does not remove the shared network namespace.

## Image and client compatibility

The published image index supports AMD64 and ARM64:

```text
ghcr.io/pvarki/tak-server:5.8.69-260913-slimignite-pr136@sha256:5d250adfdb835e9bfafcce294736041d4fbd32c1ffde7b803f17e28bf2f85a09
```

[The server manifests](../deploy/base/takserver.yaml) and
[the operator's JAR source](../Dockerfile_temurin) still pin PR #134. Update both
as part of the migration and compare the packaged `UserManager.jar` checksum.
[Upstream validation](https://github.com/pvarki/docker-atak-server/blob/025355ba9f76bf6c41187897f4a5fcac93b82afb/IGNITE_VALIDATION.md)
reports the unchanged JAR checksum
`329c3831d0550bef309a7c77ff29c3b398279f973bc9789e0db5c26817ac39c7` for its local
validation build. This review inspected the published image index, not its JAR.

[Our JNI runtime](../src/takoperator/runtime.py) already has the needed separation:
its own bind IP, private TAKCL XML, a remote discovery override, a writable Ignite
work directory, and no server-volume mount. Preserve that client configuration.
TAKCL needs unicast discovery; do not copy the server's new multicast-enabled XML
into the operator. Make the client's unicast choice explicit when updating its
configuration tests. Point `TAK_IGNITE_HOST` at the new messaging discovery Service.
No plugin-launcher workaround is needed in this Python process; the PR's launcher
is specifically for the optional TAK plugin-manager service.

## Coordinated manifest changes

1. Replace Deployment `takserver` with separate config, messaging and API
   Deployments. Keep initialization under one owner, for example the config
   Deployment's init container. Retention and plugin manager are currently omitted
   here and can remain outside this migration.
2. Remove the mounted old Ignite template (`deploy/base/TAKIgniteConfig.tpl`
   at baseline commit `2d267fe`)
   and its ConfigMap. Use the new image's per-profile renderer. Set
   `TAK_IGNITE_BIND_ADDRESS` from each Pod's IP and `TAK_IGNITE_SEEDS` to the
   messaging discovery address. The PR's default `takmsg-ignite:47500` is a
   Compose alias that does not exist in our current Kubernetes bundle.
3. Add a messaging-only headless discovery Service with
   [`publishNotReadyAddresses: true`](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/#pods),
   so clients can discover messaging during
   bootstrap. Give API and messaging separate application Services for HTTPS
   and CoT. The current single Service selects the whole shared Pod and cannot
   route different ports to different role-specific Pods using one selector.
4. Start config and messaging together, then wait for their readiness. Config
   needs messaging's Ignite server; messaging waits for config's shared-file
   generation marker. Waiting for config readiness before starting messaging
   would deadlock. Replace config's current TCP 47500 probe with the PR's
   profile-specific health check; a config client does not own discovery.
5. Keep Ignite ingress between all participating role Pods and the operator.
   Preserve suitable common labels or update the existing NetworkPolicies,
   including CNPG's TAK-client selector. TCP 47500 reaches the messaging server;
   communication uses the advertised Pod IPs. The PR health check inspects local
   TCP 10800 as well, but that does not require exposing it to application clients.
6. Preserve our [JVM defaults hook](../deploy/base/takserver-defaults.sh) in each
   TAK service so JDBC can negotiate CNPG's TLS 1.3 minimum. Give each role its
   own writable `/opt/tak/runtime/<profile>` directory for Ignite and outbound
   trust files. Keep those files out of the shared data volume.
7. Update [Taskfile targets](../Taskfile.yml): rollout waits currently name
   `deployment/takserver`, logs name its messaging container, and `operator:up`
   checks that old Deployment. HTTPS and CoT forwarding will need separate
   Service targets. Update deployment documentation and diagnostics together.

The renderer, seed selection and runtime paths above are defined in the PR's
[Ignite startup code](https://github.com/pvarki/docker-atak-server/blob/025355ba9f76bf6c41187897f4a5fcac93b82afb/scripts/ignite_config.py)
and [template](https://github.com/pvarki/docker-atak-server/blob/025355ba9f76bf6c41187897f4a5fcac93b82afb/templates/TAKIgniteConfig.tpl).
The new [health check](https://github.com/pvarki/docker-atak-server/blob/025355ba9f76bf6c41187897f4a5fcac93b82afb/scripts/healthcheck.py)
checks listeners belonging to each role rather than sockets shared with another
container.

## Storage and placement

The PR still shares `/opt/tak/data` and retains the config owner's `flock` and
generation-marker contract. This includes certificates, CoreConfig and persisted
file authentication; CNPG does not replace that volume.

Our `tak-data` PVC is `ReadWriteOnce`.
[Separate Pods can share it on the same node](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes),
so the existing one-node kind cluster is suitable for an initial split. It does
not establish support for placing those Pods on independent production nodes.
Production needs either deliberate co-location, shared storage with verified lock
semantics, or a further change to configuration/state distribution. Simply giving
each role a private PVC would break the current startup contract.

Stop the old shared-Pod Deployment before the new config owner takes over the
same volume. Preserve the existing Secrets, CNPG Cluster and TAK data PVC during
cutover. An image/networking change does not require resetting certificates or
reimporting the database.

## Validation still needed here

- Render and server-validate the split overlay; prove each role has a distinct
  Pod IP and joins one Ignite server, with the expected clients.
- Test fresh startup and repeat `task up`, including discovery before Pods become
  ready. Keep the config/messaging dependency from becoming a readiness cycle.
- Reconcile real users through this operator and verify persistent credentials
  and ordinary/IN/OUT groups, HTTPS, CoT, and encrypted CNPG connections.
- Replace messaging and change its Pod IP; verify other roles and this JNI client
  reconnect. Repeat independent config/API restarts and check retained state.
- Verify the chosen cache budget under CoT traffic. Our old template caps it at
  128 MiB; upstream validated messaging at 256 MiB and observed failures at
  64 MiB. The PR does not establish whether our 128 MiB setting is sufficient.
- Validate NetworkPolicy enforcement and storage locking on the intended
  production CNI/storage. Neither the PR's Compose results nor our one-node kind
  environment demonstrates cross-node storage behavior.

The original review compared PR source and CI results, inspected the published image
index, and rendered the existing local Kustomize bundle. It did not deploy the
new image or test split Kubernetes Pods. The shared-Pod manifests were the
deployed baseline at the time of that review.
