# Greenfield TAK Kubernetes Operator — JNI Architecture Plan

**Target:** TAK Server `5.8-RELEASE-69`
**Validated image:** `ghcr.io/pvarki/tak-server:5.8.69-2609122002-pr134`
**Implementation:** new Python operator, new PyJNIus bridge, no dependency on `python-tak-rmapi` JNI helper code

## 1. Scope

The operator is implemented greenfield.

Existing RMAPI JNI code and the archived live-tracing scripts are **test evidence only**. They define known TAK 5.8.69 behavior and PyJNIus interoperability constraints, but the operator should not import, wrap, or preserve their API.

The operator should own a purpose-built JNI layer with:

- deterministic JVM bootstrap
- typed Python snapshots
- direct access to TAK's Ignite-backed Java interfaces
- no parsing of human-readable `OnlineFileAuthModule` result strings
- no reliance on helper booleans
- explicit reconciliation semantics

## 2. Keep three TAK concepts separate

| Concept | TAK representation | Examples | Operator subsystem |
|---|---|---|---|
| File-auth server privilege | `UserAuthenticationFile.User.role` / `com.bbn.marti.xml.bindings.Role` | `ROLE_ADMIN`, `ROLE_ANONYMOUS` | file-auth bridge; internal/admin concern |
| Server routing/auth group | `groupList`, `groupListIN`, `groupListOUT` | deployment-defined names | file-auth bridge |
| Operational TAK role | CoT `detail.__group.role` | `Team Member`, `Team Lead`, `Medic`, `RTO` | operational identity bridge |

The Kubernetes Role resource in this project means the **operational TAK role**, not `ROLE_ADMIN`.

## 3. Live-validated TAK 5.8.69 facts

The archived isolated test used:

- the exact target image
- a fresh PostgreSQL database
- real TAK service JVMs
- real Ignite
- PyJNIus
- the unmodified production JARs/classes

The final lifecycle suite passed 48 checks; a fresh JNI process after restarting TAK services passed another 8 checks.

Confirmed file-auth behavior:

- password users create/update/delete correctly
- password storage is hashed
- certificate CN and SHA-256 fingerprint resolution work
- duplicate certificate registration remains one user
- ordinary groups work and disappear when the last member is removed
- directional IN/OUT groups work
- all seven `com.bbn.marti.xml.bindings.Role` values serialize
- direct null server-role clearing works
- a role-less user's *effective getter value* is `ROLE_ANONYMOUS`
- persistence survives restart of all TAK JVMs
- `getUsersWithGroups()` omits directional groups
- `getGroupNames()` includes directional group names
- `Direction.IN.getValue() == 1`, `Direction.OUT.getValue() == 2`

Operational roles such as `Medic` were **not** validated by that suite.

## 4. JVM bootstrap

Create a new `TakJvmRuntime` owned by the operator process.

Responsibilities:

1. Create temporary directories.
2. Generate valid `TAKCLConfig.xml` with the XML declaration at byte 0.
3. Configure `jnius_config` before importing `jnius`.
4. Configure all Java 17 module opens required by TAK/Ignite.
5. Set the `UserManager.jar` classpath.
6. Start the JVM by loading required classes.
7. Build the CLI-based server profile with:
   - base `CLIImmutableServerProfiles.SERVER_CLI.getServer()`
   - host appropriate to deployment
   - empty profile identifier where required so TAK resolves `/opt/tak/data/TAKIgniteConfig.xml`
8. Obtain the user manager directly:
   ```python
   TakclIgniteHelper.getUserManager(profile)
   ```
9. Keep the JVM and Ignite client alive for the entire operator process.
10. Close the associated Ignite instance only during process shutdown.

Do not fork after JVM startup.

## 5. Core Java classes

The greenfield file-auth bridge should load these explicitly:

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

`OnlineFileAuthModule` is optional. Prefer lower-level interfaces for reconciliation. Use a high-level module only when it provides a capability that is otherwise awkward or unavailable remotely.

## 6. Greenfield Python API

Recommended internal API:

```python
@dataclass(frozen=True)
class FileAuthUserState:
    identifier: str
    fingerprint: str | None
    explicit_server_role: str | None
    effective_server_role: str
    groups: frozenset[str]
    in_groups: frozenset[str]
    out_groups: frozenset[str]


class TakFileAuthStore:
    def get_user(self, identifier: str) -> FileAuthUserState | None: ...
    def create_password_user(self, identifier: str, password: str | None) -> FileAuthUserState: ...
    def create_certificate_user(self, certificate_path: Path) -> FileAuthUserState: ...
    def replace_existing_user_state(self, desired: FileAuthUserState) -> FileAuthUserState: ...
    def delete_user(self, identifier: str) -> None: ...


class TakOperationalIdentityStore:
    def get_observed_identity(self, uid: str): ...
    def configure_role(self, subject, role: str): ...
    def configure_team(self, subject, team: str): ...
```

Reconciler code must never manipulate Java objects directly.

## 7. Preferred existing-user update primitive

For an **existing** user, prefer the overloaded remote operation:

```java
FileUserManagementInterface.addOrUpdateUser(
    User desired,
    boolean passwordHashed,
    User oldUser
)
```

Why:

- the 5.8.69 `FileAuthenticator.addOrUpdateUser(User,...)` computes diffs for:
  - password
  - fingerprint
  - server auth role
  - ordinary groups
  - IN groups
  - OUT groups
- directional additions are handled internally with `Direction.IN` / `Direction.OUT`
- removals are handled internally
- `DistributedUserManager` calls `FileAuthenticator.saveChanges(control)` after the method returns
- the entire desired user replacement is one Ignite remote mutation instead of a sequence of separate client-side calls

The live `certmod` test exercised this path for ordinary, IN, and OUT memberships successfully.

### Python implementation pattern

Create a **new** `UserAuthenticationFile$User` object representing desired state rather than mutating the live object returned by `getFirstUser()`.

Copy/assign:

- identifier
- password/passwordHashed only when intentionally changing password
- fingerprint
- server auth role, including Java null when clearing
- `groupList`
- `groupListIN`
- `groupListOUT`

Pass a snapshot/copy of the previous user as the third argument.

After the call, read the user back and compare with desired state.

## 8. User creation must be a separate phase

Do not depend on the new-user branch of `addOrUpdateUser(User,...)` to initialize all membership categories.

The JAR's creation branch has special group/default behavior.

Greenfield sequence:

### Password user

1. `addOrUpdateUser(identifier, password, False)`
2. read user back
3. if groups/fingerprint/server privilege need additional changes, call the existing-user replacement primitive

### Certificate user

1. load X.509 certificate with `SSLHelper`
2. `addOrUpdateUserFromCertificate(certificate)`
3. read user back
4. apply additional desired state with the existing-user replacement primitive

This yields deterministic behavior independent of creation defaults.

## 9. Persistence and atomicity

### Separate low-level mutations

Methods such as:

```text
addOrUpdateUser(String,...)
addOrUpdateUserFromCertificate(...)
removeUser(...)
addUserToGroup(...)
removeUserFromGroup(...)
setUserRole(...)
setUserFingerprint(...)
```

each persist before returning through `DistributedUserManager`.

Therefore:

```python
manager.setUserFingerprint(...)
manager.addUserToGroup(...)
manager.setUserRole(...)
```

is **three separately persisted operations**.

`manager.saveChanges(None)` afterward is not a commit boundary.

### Single replacement call

`addOrUpdateUser(User desired, ..., User old)` performs its internal user/group diff server-side and then the distributed manager saves once after the remote operation returns.

Use this whenever reconciling several fields of an existing user.

This reduces, but does not create, a distributed transaction guarantee.

## 10. Locking

Use one process-level `threading.RLock` around all file-auth mutations.

The lock guarantees:

- no two reconciliation threads in the same operator process interleave TAK mutations

It does **not** guarantee:

- database transaction semantics
- rollback
- atomicity across different remote calls
- exclusion against another operator Pod or external TAK admin

Across replicas, use Kubernetes leader election so only the leader performs mutations.

## 11. Read model

Never reconstruct complete user state from `getUsersWithGroups()`.

For each user use:

```python
user = manager.getFirstUser(identifier)
```

Read:

```text
user.getIdentifier()
user.getFingerprint()
user.getRole()
user.getGroupList()
user.getGroupListIN()
user.getGroupListOUT()
```

Reason: live testing proved `getUsersWithGroups()` includes ordinary groups but omits directional memberships.

## 12. Server-auth role representation

Two persisted XML states can have the same effective getter value:

```xml
<User identifier="x" ... />
```

and:

```xml
<User identifier="x" ... role="ROLE_ANONYMOUS" />
```

For a role-less user:

```text
getUserRole(x) == ROLE_ANONYMOUS
```

because the JAXB getter supplies the default.

The bridge must therefore track:

- effective server role
- whether the role was explicitly stored

For exact representation, inspect:

```python
user.getRole()
```

on the raw JAXB `User`, not just `manager.getUserRole()`.

Operational Role CRs must not use either field.

## 13. PyJNIus typing rules

The greenfield bridge should centralize conversion helpers.

For boxed Boolean:

```python
Boolean = autoclass("java.lang.Boolean")
TRUE = Boolean(True)
FALSE = Boolean(False)
```

The live environment showed `Boolean.TRUE` did not resolve correctly for `certmod`.

For Java varargs, pass positional strings:

```python
module.addUserToGroups(user, group)
```

Do not pass a Python list to a varargs position unless the exact method is an explicit array parameter.

The greenfield bridge should avoid exposing these details to controller code.

## 14. Reconciliation failure model

Any JNI exception after a series of separate operations must be treated as potentially partial success.

Algorithm:

1. catch and classify Java exception
2. re-read TAK state
3. update status with observed state
4. return retryable failure where appropriate
5. next reconciliation recomputes the remaining diff

Do not guess which prior calls committed.

## 15. Operational roles remain a separate subsystem

`Team Lead`, `Medic`, `RTO`, etc. are CoT/client operational state.

Do not implement them with:

```python
manager.setUserRole(...)
```

The live JNI archive explicitly validates file-auth behavior only; it did not validate durable operational-role configuration.

The operational-role implementation requires separate 5.8.69 JAR tracing for:

- device profiles
- client configuration
- latest CoT/contact state
- possible CoT injection

Prefer a durable client-configuration path over repeated CoT injection.

## 16. Test strategy

Build new operator tests directly against the archived fixture behavior.

### JNI unit/interface tests

- class loading
- Java collection conversion
- boxed types
- role-null representation
- user snapshot conversion

### Live integration tests

- create password user
- create certificate user
- replace full existing user state
- ordinary + IN + OUT membership replacement
- admin promotion/demotion if needed internally
- delete
- repeated no-op reconcile
- process restart
- TAK service restart

### Failure injection

- stop TAK messaging between reads and writes
- crash controller between two separate mutations
- concurrent Kubernetes updates
- leader failover
- external TAK-side drift
