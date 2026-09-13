"""Kubernetes input handling and cancellation of serialized JNI operations."""

import asyncio
import base64
import json
import secrets
import threading
from datetime import datetime, timezone
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import Mock

import pytest
from cloudcoil.apimachinery import ObjectMeta
from cloudcoil.controller import Context
from cloudcoil.errors import ResourceConflict, ResourceNotFound
from cloudcoil.models.kubernetes.core.v1 import Secret

from takoperator.app import create_application
from takoperator.bridge import FileAuthUserState
from takoperator.controllers import (
    CREDENTIAL_SECRET,
    DELETION_POLICY,
    OBSERVED,
    OWNERSHIP,
    SerializedExecutor,
    UserController,
    credential_from_secret,
    ownership_for,
    referenced_users,
)
from takoperator.models import Group, GroupSpec, ObjectRef, Role, User, UserSpec
from takoperator.reconciliation import Ownership, ReconcileError, ReconcileOutcome, UserReconciler

TEST_CREDENTIAL = secrets.token_urlsafe(24)


def user_resource(**changes: Any) -> User:
    """A real platform resource shape, including foreign fields and status."""
    spec = {
        "callsign": "alpha",
        "publicKey": "platform-key",
        "approvedAt": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "approvalCode": "platform-owned",
        **changes,
    }
    return User(
        metadata=ObjectMeta(
            name="alpha",
            uid="user-uid",
            resource_version="1",
            generation=3,
            annotations={CREDENTIAL_SECRET: str(credential_secret().name), "other.example/value": "preserve"},
        ),
        spec=UserSpec.model_validate(spec),
        status={"conditions": [{"type": "Ready", "status": "True"}], "roles": []},
    )


def credential_secret(data: dict[str, str] | None = None) -> Secret:
    return Secret(
        metadata=ObjectMeta(name="alpha-credential", namespace="takserver", uid="secret-uid", resource_version="2"),
        data=data if data is not None else {"password": base64.b64encode(TEST_CREDENTIAL.encode()).decode("ascii")},
    )


class FakeContext:
    """Live Kubernetes reads without a network or an informer."""

    def __init__(self, user: User, *, secret: Secret | None = None, groups: tuple[Group, ...] = ()) -> None:
        self.user = user.model_copy(deep=True)
        self.secret = secret
        self.groups = {group.name: group for group in groups}
        self.others = [self.user]

    async def get(self, resource: Any, name: str, *, namespace: str | None = None) -> Any:
        if resource is User:
            return self.user
        if resource is Secret and self.secret and namespace == "takserver":
            return self.secret
        if resource is Group and name in self.groups:
            return self.groups[name]
        raise ResourceNotFound("test resource absent", status_code=404)

    async def client(self, resource: Any) -> "FakeContext":
        assert resource is User
        return self

    async def list(self) -> "FakeContext":
        return self

    async def __aiter__(self) -> AsyncIterator[User]:
        for user in self.others:
            yield user

    def typed(self) -> Context[User]:
        return cast(Context[User], self)


def test_consumed_models_and_manifests_preserve_platform_ownership() -> None:
    user = user_resource()
    exported = user.model_dump(by_alias=True)
    assert exported["spec"]["approvalCode"] == "platform-owned"
    assert exported["status"] == user.status
    app = create_application()
    manifests = app.manifests()
    assert not any(obj["kind"] == "CustomResourceDefinition" for obj in manifests)
    assert app.leader_election is not None
    rules = [rule for obj in manifests for rule in obj.get("rules", [])]
    assert not any(resource.endswith("/status") for rule in rules for resource in rule.get("resources", []))
    assert {model.gvk().kind for model in (User, Group, Role)} == {"User", "Group", "Role"}


def test_credentials_are_ephemeral_and_versioned() -> None:
    credential = credential_from_secret(credential_secret())
    assert credential.value == TEST_CREDENTIAL
    assert credential.revision == "secret-uid:2"
    assert credential.value not in repr(credential)
    certificate = credential_from_secret(credential_secret({"tls.crt": base64.b64encode(b"certificate").decode()}))
    assert certificate.kind == "certificate"


@pytest.mark.parametrize("case", ["missing", "invalid-base64", "empty", "ambiguous"])
def test_invalid_credentials_fail_closed(case: str) -> None:
    data: dict[str, str] = {}
    if case != "missing":
        data["password"] = "!" if case == "invalid-base64" else ""
    if case == "ambiguous":
        data["password"] = base64.b64encode(TEST_CREDENTIAL.encode()).decode()
        data["tls.crt"] = base64.b64encode(b"certificate").decode()
    with pytest.raises(ValueError):
        credential_from_secret(credential_secret(data))


@pytest.mark.asyncio
async def test_checkpoint_is_returned_without_changing_platform_status() -> None:
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    engine = Mock(spec=UserReconciler)
    engine.reconcile.return_value = ReconcileOutcome(
        Ownership(identifier="alpha"), None, "OwnershipPending", False, True
    )
    handler.engine = engine
    user = user_resource()
    original_status = user.status
    result = await handler.reconcile(user, FakeContext(user, secret=credential_secret()).typed())
    assert result.requeue_after == 1
    assert ownership_for(user) == Ownership(identifier="alpha")
    assert user.status == original_status
    assert user.metadata and user.metadata.annotations
    assert user.metadata.annotations["other.example/value"] == "preserve"
    assert TEST_CREDENTIAL not in user.model_dump_json()
    await executor.close()


@pytest.mark.asyncio
async def test_missing_group_removes_managed_edge_and_reports_pending() -> None:
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    engine = Mock(spec=UserReconciler)
    engine.reconcile.return_value = ReconcileOutcome(
        Ownership(identifier="alpha"), FileAuthUserState("alpha"), "Synced", True
    )
    handler.engine = engine
    user = user_resource(groupRefs=[{"name": "gone"}], roleRefs=[{"name": "medic"}])
    await handler.reconcile(user, FakeContext(user, secret=credential_secret()).typed())
    assert engine.reconcile.call_args.kwargs["groups"] == frozenset()
    assert user.metadata and user.metadata.annotations
    observation = json.loads(user.metadata.annotations[OBSERVED])
    assert observation["reason"] == "GroupNotFound"
    assert observation["operationalRoles"] == "Unsupported"
    assert observation["ready"] is False
    await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["secret", "immutable", "duplicate", "invalid-ownership", "changed"])
async def test_unsafe_inputs_never_reach_tak(case: str) -> None:
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    engine = Mock(spec=UserReconciler)
    handler.engine = engine
    user = user_resource()
    assert user.metadata and user.metadata.annotations
    context = FakeContext(user, secret=credential_secret() if case != "secret" else None)
    if case == "immutable":
        user.metadata.annotations[OWNERSHIP] = Ownership(identifier="old-name").model_dump_json()
    if case == "invalid-ownership":
        user.metadata.annotations[OWNERSHIP] = "broken"
    if case == "duplicate":
        other = user_resource()
        assert other.metadata
        other.metadata.uid = "other-uid"
        context.others.append(other)
    if case == "changed":
        assert context.user.metadata
        context.user.metadata.resource_version = "99"
        with pytest.raises(ResourceConflict):
            await handler.reconcile(user, context.typed())
    else:
        await handler.reconcile(user, context.typed())
    engine.reconcile.assert_not_called()
    await executor.close()


@pytest.mark.asyncio
async def test_revocation_does_not_need_a_secret_and_finalize_uses_owned_identity() -> None:
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    engine = Mock(spec=UserReconciler)
    engine.reconcile.return_value = ReconcileOutcome(Ownership(identifier="alpha"), None, "Inactive", False)
    handler.engine = engine
    user = user_resource(revokedAt=datetime(2026, 9, 2, tzinfo=timezone.utc))
    await handler.reconcile(user, FakeContext(user).typed())
    assert engine.reconcile.call_args.kwargs["active"] is False
    assert engine.reconcile.call_args.kwargs["credential"] is None
    await handler.finalize(user)
    engine.finalize.assert_called_once_with(Ownership(identifier="alpha"), retain=False)
    await executor.close()


@pytest.mark.asyncio
async def test_partial_failure_publishes_observed_state() -> None:
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    engine = Mock(spec=UserReconciler)
    engine.reconcile.side_effect = ReconcileError(
        "TakUnavailable", "retry", FileAuthUserState("alpha", groups=frozenset({"one"}))
    )
    handler.engine = engine
    user = user_resource()
    await handler.reconcile(user, FakeContext(user, secret=credential_secret()).typed())
    assert user.metadata and user.metadata.annotations
    observation = json.loads(user.metadata.annotations[OBSERVED])
    assert observation["state"]["groups"] == ["one"]
    assert observation["ready"] is False
    await executor.close()


@pytest.mark.asyncio
async def test_invalid_deletion_policy_keeps_finalizer_without_mutation() -> None:
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    engine = Mock(spec=UserReconciler)
    handler.engine = engine
    user = user_resource()
    assert user.metadata and user.metadata.annotations
    user.metadata.annotations[OWNERSHIP] = Ownership(identifier="alpha").model_dump_json()
    user.metadata.annotations[DELETION_POLICY] = "retain"
    with pytest.raises(ReconcileError) as caught:
        await handler.finalize(user)
    assert caught.value.reason == "InvalidDeletionPolicy"
    engine.finalize.assert_not_called()
    user.metadata.annotations[DELETION_POLICY] = "Retain"
    await handler.finalize(user)
    engine.finalize.assert_called_once_with(Ownership(identifier="alpha"), retain=True)
    await executor.close()


def test_dependency_keys_and_group_schema() -> None:
    user = user_resource(groupRefs=[{"name": "blue"}])
    assert [key.name for key in referenced_users([user], "groups", "blue")] == ["alpha"]
    assert referenced_users([user], "roles", "blue") == []
    group = Group(spec=GroupSpec(name="routing-blue", role_refs=[ObjectRef(name="medic")]))
    assert group.spec.role_refs[0].name == "medic"


@pytest.mark.asyncio
async def test_cancellation_waits_for_jni_and_serializes_next_operation() -> None:
    executor = SerializedExecutor()
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def blocked() -> None:
        entered.set()
        assert release.wait(timeout=5)
        calls.append("first")

    first = asyncio.create_task(executor.run(blocked))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    first.cancel()
    second = asyncio.create_task(executor.run(calls.append, "second"))
    await asyncio.sleep(0.01)
    assert not first.done()
    assert calls == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert calls == ["first", "second"]
    await executor.close()
    with pytest.raises(RuntimeError, match="closed"):
        await executor.run(lambda: None)
