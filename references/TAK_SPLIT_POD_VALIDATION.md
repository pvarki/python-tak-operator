# TAK split-Pod validation

Validated 2026-09-13 on the shared ARM64 `kind-rmk8soperator` cluster, namespace
`tak-operator-system`, with the CNPG PostgreSQL/PostGIS database.

## Image and storage

Server and operator JAR source:

```text
ghcr.io/pvarki/tak-server:5.8.69-260913-slimignite-pr136@sha256:5d250adfdb835e9bfafcce294736041d4fbd32c1ffde7b803f17e28bf2f85a09
```

The published image's `UserManager.jar` checksum matched the previous image and
the upstream validation: `329c3831d0550bef309a7c77ff29c3b398279f973bc9789e0db5c26817ac39c7`.
The migration preserved both PVC UIDs and the CA/admin certificate checksums.
No database import or certificate reset was needed. All roles mounted the same
ReadWriteOnce data volume on the config Pod's node; each used a private runtime
`emptyDir`.

## Successful checks

- Kustomize rendering and Kubernetes server-side dry runs passed for TAK and
  the operator. `task tak:up` retired the legacy shared-Pod Deployment and
  started config, messaging and API with one service container per Pod.
- Initial TAK Pod IPs were `10.244.0.21`, `.23` and `.22`, respectively.
  Ignite reported one server and three clients after the separate JNI operator
  joined. Its JAR was loaded from the rebuilt production image.
- The sibling's Bob and Charlie demo Users reconciled with generated credential
  Secrets and `group-admin` membership. Removing and restoring Bob's group
  reconciled at the current User generation. Persisted file-auth state contained
  both users, their expected groups and hashed credentials.
- Verified HTTPS over TLS 1.3 through `takserver:8443`, including certificate
  hostname/CA verification and the expected `/Marti/` login redirect.
- Two mutually authenticated TLS clients connected through
  `tak-messaging:8089`; a CoT event sent by one arrived at the other.
- CNPG's `pg_stat_ssl` reported TLS 1.3 for TAK's `martiuser` connections.
- `task tak:restart` stopped all TAK Pods before recreating the grid and
  reconnecting the operator. Accounts and membership state survived recovery.
- Independent config and API Pod replacements rolled out successfully while
  messaging stayed running; HTTPS and CoT exchange passed again afterward.
- Repeat `task up` completed successfully and preserved all three TAK Pod UIDs.
- The unit suite passed: 67 tests, 88.32% coverage. Regression tests cover
  cutover validation/deletion failure, discovery before readiness, Service
  routing, shared-volume ownership, Pod affinity and private JNI binding.

The current sibling User schema no longer requires `publicKey`. Live testing
exposed that our old consumer model rejected those Users; the compatibility fix
accepts omitted/null keys while preserving legacy values. TAK credentials still
come from the explicitly referenced Secret.

## Messaging replacement limitation

Replacing only messaging changed its Pod IP from `10.244.0.23` to `.26`.
The operator rejoined, but config/API failed to complete reconnection and
messaging did not finish starting its application listeners. Both clients had
established TCP connections to the new discovery endpoint, so this was not a
stale DNS address. API logged blocked Ignite discovery workers, including
`ConnectGateway.disconnected` waiting in `GridSpinReadWriteLock.writeLock`.
The configured upstream `NoOpFailureHandler` suppressed the failure.

The image's health check verifies local listeners and the config owner's
shared-file lock. Those checks left the stalled clients ready; they do not
demonstrate a working Ignite RPC path. Use `task tak:restart` for coordinated
recovery. Transparent messaging failover remains an upstream/JVM integration
issue; this migration does not claim to solve it.

The local kind CNI does not enforce NetworkPolicy. Cross-node storage locking,
production CNI enforcement and sustained load remain outside this validation.
