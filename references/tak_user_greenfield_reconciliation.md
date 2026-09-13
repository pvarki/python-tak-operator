# Greenfield TAK User Reconciliation Plan

**Target:** TAK Server 5.8.69
**JNI implementation:** new operator-owned bridge

## 1. Resource responsibility

A User resource manages TAK Server file-auth identity state:

- identifier
- password-backed or certificate-backed identity
- fingerprint
- server routing/auth memberships
- optional internal server-admin privilege if the CRD explicitly models it

Operational roles such as `Medic` and `Team Lead` are not `UserAuthenticationFile.role`; they are reconciled by the operational-identity subsystem.

## 2. Desired-state model

Recommended internal model:

```python
@dataclass(frozen=True)
class DesiredFileAuthUser:
    identifier: str
    credential_type: Literal["password", "certificate"]
    fingerprint: str | None
    groups: frozenset[str]
    in_groups: frozenset[str]
    out_groups: frozenset[str]

    # Optional internal TAK Server privilege only.
    explicit_server_role: str | None = None
```

Do not place password material into this long-lived snapshot. Resolve the Secret only at mutation time.

## 3. Actual-state read

Primary read:

```python
java_user = manager.getFirstUser(identifier)
```

If null, user is absent.

For present users read all three membership lists directly:

```python
groups = set(java_user.getGroupList().toArray())
in_groups = set(java_user.getGroupListIN().toArray())
out_groups = set(java_user.getGroupListOUT().toArray())
```

Read fingerprint:

```python
java_user.getFingerprint()
```

For exact stored server-role state:

```python
explicit_role = java_user.getRole()
```

For effective auth role:

```python
manager.getUserRole(identifier)
```

Do not use `getUsersWithGroups()` as the authoritative user snapshot because it omits IN/OUT memberships.

## 4. Create password user

Create base identity:

```python
manager.addOrUpdateUser(identifier, password, False)
```

This call persists before returning.

Then read the user back.

If desired state also contains fingerprint, groups, directional groups, or explicit server privilege, apply those through `replace_existing_user_state()`.

Do not assume automatic `__ANON__` membership; live testing showed string-based creation had no groups in the validated configuration.

## 5. Create certificate user

Load certificate:

```python
certificate = SSLHelper.getCertificate(str(cert_path))
```

Resolve identity before mutation:

```python
cert_identifier = SSLHelper.getCertificateUserName(certificate)
expected_fingerprint = SSLHelper.loadCertFingerprintForEndUser(str(cert_path))
```

Validate that the CRD subject matches `cert_identifier`.

Create/update:

```python
manager.addOrUpdateUserFromCertificate(certificate)
```

The remote call persists before returning.

Re-read and verify:

- identifier exists
- fingerprint equals expected SHA-256 fingerprint

Duplicate certificate registration is idempotent in the validated server.

## 6. Replace existing user state

Use the overloaded remote method:

```java
addOrUpdateUser(User desired, boolean passwordHashed, User oldUser)
```

This is the preferred greenfield update primitive for existing users.

### Build a desired JAXB user

Create:

```python
User = autoclass(
    "com.bbn.marti.xml.bindings.UserAuthenticationFile$User"
)
desired = User()
```

Copy stable/current fields that are not managed by this reconciliation.

Set managed fields from desired Kubernetes state.

Populate lists by mutating the Java lists returned from:

```text
desired.getGroupList()
desired.getGroupListIN()
desired.getGroupListOUT()
```

### Old-state snapshot

Build a separate Java `User` copy from the current user.

Do not mutate the `getFirstUser()` object in place before handing it back to the server.

### Execute

```python
manager.addOrUpdateUser(desired, False, old_copy)
```

The server computes group-direction diffs and the distributed manager persists the returned control once.

Then re-read the user.

## 7. Password updates

When password changes, either:

- use the string overload directly, then re-read, or
- include the raw password and `passwordHashed=False` in a complete existing-user replacement

Prefer the simplest tested path unless a single full replacement is specifically validated for password + group changes.

Never compare or expose stored hashes as desired-state material.

## 8. Fingerprint updates

For a fingerprint-only change:

```python
manager.setUserFingerprint(identifier, fingerprint)
```

For a multi-field existing-user reconcile, include fingerprint in the replacement user instead.

Live validation confirmed JNI state and XML agree after fingerprint mutation.

## 9. Server administrator privilege

This is an internal authentication-management concern, not an operational role.

For greenfield code, do not parse human-readable `OnlineFileAuthModule` strings.

Promotion can be done directly with:

```python
Role = autoclass("com.bbn.marti.xml.bindings.Role")
manager.setUserRole(identifier, Role.ROLE_ADMIN)
```

For a certificate user, ensure the certificate identity exists first.

Demotion to an ordinary role-less representation:

```python
manager.setUserRole(identifier, None)
```

Do not use the high-level `OnlineFileAuthModule.setUserRole(..., None)` because 5.8.69 saves and then throws `NullPointerException`.

### Reconciliation subtlety

After null clear:

```text
raw java_user.getRole() == null
manager.getUserRole(identifier) == ROLE_ANONYMOUS
```

Therefore "no explicit server role" must be tested using the raw JAXB user.

## 10. Authorization groups

If User owns memberships, reconcile:

- ordinary
- IN
- OUT

as one desired state through the overloaded user replacement method where possible.

If a separate Group CR owns membership, the User controller must not also enforce it.

Choose one owner for each membership edge.

## 11. Delete

Use:

```python
if manager.userExists(identifier):
    manager.removeUser(identifier)
```

This persists before returning.

Then verify:

```python
not manager.userExists(identifier)
```

Deletion finalizer policy:

- `Delete`: remove TAK user, verify absent, remove finalizer
- `Retain`: skip TAK delete, remove finalizer

## 12. Rename

TAK exposes no identity rename primitive.

Make identifier immutable.

A rename should require a new CR or an explicit delete-and-recreate workflow.

## 13. Idempotency

Every reconcile:

1. read actual
2. derive desired
3. compare
4. no-op if equal
5. issue smallest safe mutation
6. re-read
7. update status

Never use method return strings/booleans as truth when actual state can be read.

## 14. Status

Example:

```yaml
status:
  observedGeneration: 9
  identifier: tak.example.example.org
  fingerprint: "AA:BB:..."
  groups:
    ordinary:
      - operations
    in:
      - mission-in
    out:
      - mission-out
  serverPrivilege:
    effectiveRole: ROLE_ANONYMOUS
    explicitRole: null
  conditions:
    - type: Ready
      status: "True"
    - type: Synced
      status: "True"
```

Operational role should be reported under a separate operational-identity status block if the User CR references it.

## 15. Live acceptance tests

- password create
- password update
- certificate create
- duplicate certificate create
- fingerprint update
- ordinary groups
- IN/OUT groups
- full existing-user replacement
- null server-role clear
- delete
- restart persistence
- no-op second reconcile
- reconcile after deliberate partial failure
