# TAK Kubernetes Operator JNI — Handoff

## Purpose

This handoff captures the current design state for a **greenfield Python Kubernetes operator for TAK Server 5.8.69** using an embedded JVM via PyJNIus and TAK's Ignite-backed Java services.

The operator will **not reuse the existing RMAPI JNI helper implementation**. Existing RMAPI/JNI code and the archived live-tracing bundle are evidence/reference material only.

## Target environment

- TAK Server: `5.8-RELEASE-69`
- Validated image: `ghcr.io/pvarki/tak-server:5.8.69-2609122002-pr134`
- Image digest: `sha256:04d58dd4b1154ec5232f27131c03affabbf98e6dbc39a4f88ba53066db46e013`
- `UserManager.jar` SHA-256: `329c3831d0550bef309a7c77ff29c3b398279f973bc9789e0db5c26817ac39c7`
- Java: 17
- JNI: PyJNIus
- User-management transport: Ignite through `TakclIgniteHelper`

## Core architectural decision

Build a new operator-owned JNI layer with:

- one JVM per operator process
- one long-lived Ignite session
- no process forking after JVM startup
- typed Python state objects at the controller boundary
- raw Java objects confined to the JNI bridge
- direct use of `FileUserManagementInterface` for file-auth reconciliation
- Kubernetes leader election between replicas
- process-level serialization of mutations
- re-read-after-write reconciliation instead of trusting textual helper results

## Keep three TAK concepts separate

### 1. TAK Server authentication/management role

Stored in:

```text
UserAuthenticationFile.User.role
```

Java type:

```text
com.bbn.marti.xml.bindings.Role
```

Examples include `ROLE_ADMIN`, `ROLE_ANONYMOUS`, and `ROLE_READONLY`.

This is **not** the Kubernetes operational Role resource.

### 2. TAK Server authorization/routing groups

Stored in:

```text
groupList
groupListIN
groupListOUT
```

Managed through `FileUserManagementInterface`.

### 3. TAK operational role

Examples:

```text
Team Member
Team Lead
Medic
RTO
Forward Observer
K9
```

Represented in CoT:

```xml
<detail>
  <__group name="Cyan" role="Medic"/>
</detail>
```

This is the intended meaning of the Kubernetes Role resource.

Do **not** implement operational roles with `FileUserManagementInterface.setUserRole()`.

## Live-validated TAK 5.8.69 behavior

The archived investigation used the exact target image, a fresh PostgreSQL database, real TAK service JVMs, real Ignite, PyJNIus, and unmodified production classes.

Validation completed successfully with:

- 48 lifecycle checks
- 8 fresh-process checks after restarting all TAK services

### Users

Confirmed:

- password user create/update/delete works
- passwords are hashed
- certificate CN resolution works
- SHA-256 fingerprint resolution works
- duplicate certificate registration keeps one user
- fingerprint changes persist
- state survives TAK service restarts

### Server-auth roles

Confirmed:

- all seven fixed `Role` enum values serialize
- direct `manager.setUserRole(username, None)` clears the XML role attribute
- after clear, `manager.getUserRole(username)` returns effective `ROLE_ANONYMOUS`
- raw JAXB `User.getRole()` is required to distinguish absent XML role from explicit `ROLE_ANONYMOUS`
- `ROLE_NONEXISTENT` is a separate assignable enum value
- `OnlineFileAuthModule.setUserRole(username, None)` is defective: it persists the change and then throws `NullPointerException`

### Groups

Confirmed:

- ordinary membership add/remove works
- duplicate operations preserve membership semantics
- removing the last member removes the group from enumeration
- users are not deleted with group membership
- `Direction.IN.getValue() == 1`
- `Direction.OUT.getValue() == 2`
- ordinary, IN and OUT lists persist independently
- directional removal preserves other lists
- `getGroupNames()` includes directional group names
- `getUsersWithGroups()` omits directional memberships

Therefore directional reconciliation must inspect:

```text
User.getGroupList()
User.getGroupListIN()
User.getGroupListOUT()
```

### Creation defaults

Confirmed:

- string/password user creation produced no automatic memberships in the validated fixture
- do not assume `__ANON__` is always added

### Persistence semantics

Important correction from the live trace:

`DistributedUserManager` saves after each mutation.

Therefore calls such as:

```python
manager.setUserFingerprint(...)
manager.addUserToGroup(...)
manager.setUserRole(...)
```

are separately persisted remote operations.

A later `manager.saveChanges(None)` is **not** a batching or transaction boundary.

A Python lock prevents local interleaving but does not create distributed transaction semantics.

## Preferred existing-user reconciliation primitive

For existing users, prefer:

```java
FileUserManagementInterface.addOrUpdateUser(
    User desired,
    boolean passwordHashed,
    User oldUser
)
```

The inspected 5.8.69 implementation computes diffs for:

- password
- fingerprint
- server-auth role
- ordinary groups
- IN groups
- OUT groups

This is preferable to a long sequence of separate remote mutations when several fields change.

### Implementation requirement

Build **new JAXB `User` objects** for:

- desired state
- old-state snapshot

Do not mutate the live object returned by `getFirstUser()` in place before sending it back.

## New-user reconciliation

Treat creation separately.

### Password user

1. `addOrUpdateUser(identifier, password, False)`
2. re-read
3. apply additional desired state through existing-user replacement if needed

### Certificate user

1. load certificate with `SSLHelper`
2. derive and validate username
3. `addOrUpdateUserFromCertificate(certificate)`
4. re-read
5. apply additional desired state through existing-user replacement if needed

## Recommended greenfield JNI classes

```text
com.bbn.marti.takcl.TakclIgniteHelper
com.bbn.marti.test.shared.data.servers.CLIImmutableServerProfiles
com.bbn.marti.test.shared.data.servers.MutableServerProfile$Builder
com.bbn.marti.remote.groups.FileUserManagementInterface
com.bbn.marti.xml.bindings.UserAuthenticationFile$User
com.bbn.marti.xml.bindings.Role
com.bbn.marti.remote.groups.Direction
com.bbn.marti.takcl.SSLHelper
java.lang.Boolean
```

`OnlineFileAuthModule` should be optional rather than the primary reconciliation API.

## JVM bootstrap requirements

The operator must:

1. configure `jnius_config` before importing `jnius`
2. use `/opt/tak/utils/UserManager.jar`
3. add required Java 17 module opens
4. generate valid `TAKCLConfig.xml`
5. ensure its XML declaration starts at byte 0
6. create TAKCL temporary and fallback directories
7. build the CLI server profile
8. use an empty profile identifier where required so `/opt/tak/data/TAKIgniteConfig.xml` resolves correctly
9. obtain the manager through `TakclIgniteHelper.getUserManager(profile)`
10. keep JVM/Ignite alive for the operator process lifetime
11. close the associated Ignite instance only during shutdown

## PyJNIus interoperability findings

### Boxed Boolean

Use:

```python
Boolean = autoclass("java.lang.Boolean")
Boolean(True)
Boolean(False)
```

The live environment showed `Boolean.TRUE` could fail overload resolution for boxed-Boolean signatures.

### Varargs

Positional strings worked for Java varargs:

```python
module.addUserToGroups(user, group)
```

Passing a Python list as the varargs argument failed overload resolution.

Keep all Java conversion details inside the JNI bridge.

## Resource responsibilities

### User

Manage file-auth account/certificate state.

Important rules:

- identifier should be immutable
- derive certificate identity with TAK's own helpers
- never trust helper booleans/strings when actual state can be read
- use raw `User.getRole()` to distinguish explicit vs default server role
- do not use server-role APIs for `Medic`, `Team Lead`, etc.

### Authorization Group

Manage file-auth/routing memberships.

Important rules:

- no standalone persisted group object
- empty groups may not materialize
- deletion removes owned membership edges only
- never delete users
- read ordinary, IN and OUT lists directly

### Operational Role

Represents `detail.__group.role`.

Current status:

- semantics are understood
- durable JNI CRUD path is not yet validated
- live archive did not validate operational-role persistence
- do not map it to file-auth role APIs

## Operational Role discovery still required

Inspect TAK 5.8.69 JARs for:

```text
DeviceProfile
ClientConfiguration
Profile
Contact
Subscription
Submission
Cot
TakMessage
ClientEndpoint
```

Need:

1. a read path for latest `detail.__group.name` and `detail.__group.role`
2. a durable configuration path, preferably Device Profile/onboarding/client configuration

Use direct CoT injection only if intentionally modeling ephemeral live state.

## Failure handling

Because low-level calls persist independently, assume partial success after exceptions.

On failure:

1. classify the Java exception
2. re-read TAK state
3. publish observed state
4. retry from the new observed state
5. never assume rollback

## Recommended implementation order

1. JVM/TAKCL/Ignite bootstrap
2. Java↔Python conversion helpers
3. complete user snapshot reads
4. password and certificate creation
5. existing-user full replacement
6. ordinary and directional group reconciliation
7. finalizers and leader election
8. reproduce archived live tests using the new bridge
9. add partial-failure and restart tests
10. investigate operational-role/device-profile APIs
11. implement operational Role only after persistence is proven

## Current plan documents

This bundle includes:

- `tak_operator_greenfield_architecture.md`
- `tak_user_greenfield_reconciliation.md`
- `tak_authorization_group_greenfield_reconciliation.md`
- `tak_operational_role_greenfield_reconciliation.md`

## Evidence boundary

The live archive validates file-auth users, server-auth roles, ordinary/directional groups, persistence, and PyJNIus interoperability.

It does **not** validate:

- Kubernetes reconciliation
- multi-node operator behavior
- operational-role persistence
- full FastAPI integration
- CSR-token/renewal flows
- OCSP enforcement
- directional CoT message routing

Do not mark those areas verified without new tests.
