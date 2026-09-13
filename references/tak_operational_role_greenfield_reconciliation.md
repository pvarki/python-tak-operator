# Greenfield TAK Operational Role Reconciliation Plan

**Meaning of Role:** TAK operational role such as `Team Lead`, `Medic`, `RTO`

## 1. Explicit non-goal

Do not use:

```text
com.bbn.marti.xml.bindings.Role
FileUserManagementInterface.setUserRole(...)
```

for this resource.

Those APIs manage TAK Server file-auth privileges such as:

```text
ROLE_ADMIN
ROLE_ANONYMOUS
ROLE_READONLY
```

They are unrelated to `Medic`, `Team Lead`, etc.

## 2. TAK representation

Operational role is part of the client's CoT identity:

```xml
<detail>
  <__group name="Cyan" role="Medic"/>
</detail>
```

TAK protobuf represents the group block as two strings:

```proto
message Group {
  string name = 1;
  string role = 2;
}
```

Operational team/color and operational role therefore belong to the same identity block.

## 3. Validation status

The archived TAK 5.8.69 JNI suite did **not** validate operational-role persistence or assignment.

It validated file-auth users/groups/server roles only.

Therefore this plan deliberately does not invent a JNI class/method for operational Role CRUD.

## 4. Recommended Kubernetes semantics

Model `TAKRole` as a declarative role definition/catalog:

```yaml
apiVersion: ...
kind: TAKRole
metadata:
  name: medic
spec:
  value: Medic
```

A user/device identity references it:

```yaml
spec:
  roleRef:
    name: medic
```

Creating the Role definition itself may require **no TAK mutation**.

The TAK mutation occurs when a subject is configured to use that role.

## 5. Role values

Because TAK's wire representation is a string, the operator may choose either:

### Open policy

Allow any valid non-empty role string.

### Controlled catalog

Restrict to approved operational roles, for example:

```text
Team Member
Team Lead
HQ
Sniper
Medic
Forward Observer
RTO
K9
```

If restricted, this is operator/domain policy, not a `UserAuthenticationFile.Role` enum.

## 6. Required 5.8.69 discovery before implementation

Find the durable operational-identity path in the actual TAK JARs.

Search for services/classes related to:

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

We need two capabilities.

### A. Observe actual role

Given a user/device UID or callsign, obtain the latest:

```text
detail.__group.name
detail.__group.role
```

### B. Configure desired role

Prefer a persistent mechanism that the client consumes:

1. Device Profile
2. onboarding/data-package configuration
3. another client configuration service

Only use direct CoT injection if the CRD intentionally represents transient live state.

## 7. Why CoT injection alone is risky

A connected client sends its own SA/PLI CoT.

If the server injects:

```xml
<__group role="Medic"/>
```

but the client continues sending:

```xml
<__group role="Team Member"/>
```

the observed state can drift immediately.

A reconciliation loop that repeatedly injects CoT would fight the client.

The durable design should make the client itself emit the desired role.

## 8. Desired vs observed state

Status should distinguish:

```yaml
status:
  configuredRole: Medic
  observedRole: Team Member
  clientOnline: true
```

This allows:

- configuration is correct but client has not refreshed
- configuration has drifted
- client is offline
- live state has drifted

## 9. Create

Creating a `TAKRole` definition:

1. validate `spec.value`
2. mark definition Ready
3. no TAK-side object creation unless JAR discovery proves such an object exists

Creating an assignment:

1. resolve subject
2. resolve role value
3. write persistent client configuration
4. read configuration back
5. optionally observe live CoT
6. update status

## 10. Read

Definition read: Kubernetes.

Configured assignment read: durable client configuration service.

Observed assignment read: latest client CoT/contact state.

Do not use `FileUserManagementInterface.getUserRole()`.

## 11. Update

Recommended: make `TAKRole.spec.value` immutable.

Changing a catalog definition could otherwise alter many users.

For assignment change:

```text
Team Member -> Medic
```

update persistent client configuration, then observe until the client reports `Medic`.

## 12. Delete

Deleting a Role definition must not touch file-auth server privileges.

Choose:

- block deletion while referenced, or
- require dependent assignment cleanup

Deleting an assignment should clear/reset the operational role through the same client-configuration mechanism used to create it.

## 13. Drift reconciliation

If desired `Medic` but observed `Team Lead`:

1. read persistent configured role
2. if persistent config is wrong, rewrite it
3. if config is correct, wait for/retrigger client config refresh as supported
4. do not mutate `UserAuthenticationFile.role`
5. do not blindly inject CoT in a tight loop

## 14. Team/color pairing

Operational team and role should be reconciled together when the target API permits it:

```xml
<__group name="Cyan" role="Medic"/>
```

Do not confuse `name="Cyan"` with a TAK Server routing/auth group.

## 15. Required live tests

Once the correct JNI service is found:

1. observe a real client with `Team Member`
2. write persistent desired role `Medic`
3. verify configured value through JNI
4. verify client receives configuration
5. verify subsequent client SA contains `role="Medic"`
6. restart operator
7. restart TAK Server
8. restart client
9. verify persistence
10. manually alter client role and observe drift behavior
11. verify no `UserAuthenticationFile.role` changes occur

Only after these tests should the operational Role resource be considered fully mapped to TAK.
