# Greenfield TAK Authorization Group Reconciliation Plan

**Target:** TAK Server 5.8.69

## 1. Scope

This plan is for TAK Server authorization/routing groups stored in:

```text
User.groupList
User.groupListIN
User.groupListOUT
```

It is not for the operational CoT element:

```xml
<__group name="Cyan" role="Medic"/>
```

If the Kubernetes Group CRD actually models operational team/color, it belongs with the operational identity subsystem, not this plan.

## 2. Validated TAK behavior

Live 5.8.69 testing confirmed:

- ordinary membership add/remove works
- duplicate add is membership-idempotent
- duplicate remove does not damage other users
- removing the final member removes the group from enumeration
- users are not deleted
- `Direction.IN.getValue() == 1`
- `Direction.OUT.getValue() == 2`
- separate IN and OUT memberships persist
- directional removal preserves the other direction/list
- `getGroupNames()` includes names used only in IN/OUT lists
- `getUsersWithGroups()` omits directional memberships

## 3. Group is not an independent stored object

No remote `createGroup()` / `deleteGroup()` API exists.

A group is materialized by membership.

Consequences:

- empty desired group may not appear in TAK
- deleting a Group resource means removing the membership edges owned by it
- deleting a group must never delete users

## 4. Ownership model

Recommended: Group CR owns membership.

User CR should not authoritatively manage the same membership edges.

Internally track ownership by Kubernetes UID:

```text
(group_name, direction, user_identifier) -> owner UID
```

Only remove an edge if the deleting/reconciling resource owns it.

This prevents deletion of manually managed or foreign-CR memberships.

## 5. Read actual membership

### Ordinary memberships

`getGroupsWithUsers()` is useful for ordinary memberships.

### Directional memberships

Do not rely on aggregate maps.

For each relevant user:

```python
u = manager.getFirstUser(identifier)
ordinary = set(u.getGroupList().toArray())
incoming = set(u.getGroupListIN().toArray())
outgoing = set(u.getGroupListOUT().toArray())
```

This is the authoritative view.

## 6. Preferred mutation strategy

For a group reconcile touching many users, there are two choices.

### A. Per-edge remote operations

Ordinary add:

```python
manager.addUserToGroup(user, group)
```

Ordinary remove:

```python
manager.removeUserFromGroup(user, group)
```

Directional remove:

```python
manager.removeUserFromGroup(user, group, Direction.IN)
manager.removeUserFromGroup(user, group, Direction.OUT)
```

Each call persists independently.

This is simple but not atomic.

### B. Per-user full-state replacement — preferred for directional reconciliation

For each affected user:

1. read current JAXB user
2. build desired user copy
3. update ordinary/IN/OUT lists
4. call:
   ```python
   manager.addOrUpdateUser(desired_user, False, old_copy)
   ```
5. re-read

The server-side `FileAuthenticator.addOrUpdateUser(User,...)` has internal directional add/remove support and computes all three list diffs before `DistributedUserManager` performs its save.

This avoids `certmod` and boxed-Boolean plumbing in the greenfield operator.

## 7. Create

There is no standalone creation.

A non-empty Group CR is realized by adding desired edges.

For an empty Group CR:

```yaml
status:
  materialized: false
  memberCount: 0
```

is valid.

## 8. Update

Given desired members by direction:

```python
desired_both
desired_in
desired_out
```

For each affected user, derive desired lists.

Prefer one full-user replacement per user when directional state changes.

For ordinary-only groups, per-edge calls are acceptable but still individually persistent.

## 9. Delete

On finalizer:

1. enumerate membership edges owned by this Group CR
2. remove only those edges
3. re-read all affected users
4. verify owned edges absent
5. remove finalizer

Do not expect the group name to remain after the final edge is removed.

## 10. Missing users

Before adding an edge:

```python
manager.userExists(user_identifier)
```

If absent:

- do not fabricate a user
- set `WaitingForDependency`
- retry when User CR becomes ready

Decide explicitly whether partial group membership is allowed.

Recommended default: validate all referenced users before mutation.

## 11. Direction model

Recommended CR representation:

```yaml
spec:
  members:
    - userRef:
        name: alice
      direction: BOTH
    - userRef:
        name: bob
      direction: IN
    - userRef:
        name: charlie
      direction: OUT
```

Map:

```text
BOTH -> groupList
IN   -> groupListIN
OUT  -> groupListOUT
```

Use the Java `Direction` enum only at the JNI boundary.

## 12. Reserved groups

Treat server/system group names as policy-controlled.

Do not assume `__ANON__` is automatically present: the live fixture proved string-created users can have no membership at all.

If `__ANON__` needs special policy, encode it explicitly.

## 13. Status

```yaml
status:
  observedGeneration: 6
  materialized: true
  memberships:
    both: 5
    in: 2
    out: 3
  conditions:
    - type: Ready
      status: "True"
```

## 14. Acceptance tests

- ordinary create/update/delete
- duplicate add/remove
- last-member disappearance
- no user deletion
- IN and OUT creation
- directional removal preserving other direction
- `getGroupNames` behavior
- full read from three JAXB lists
- controller restart
- failure after first affected user; next reconcile converges remaining users
