"""Exercise binding ownership, status, failure recovery and deprovisioning order."""

import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from cloudcoil.controller import get_condition, set_condition
from cloudcoil.errors import ResourceConflict

from takoperator.bindings import MANAGED_BY, UserBindings
from takoperator.bridge import FileAuthUserState
from takoperator.controllers import OBSERVED, OWNERSHIP, SerializedExecutor, UserController
from takoperator.reconciliation import Ownership, ReconcileError, ReconcileOutcome, UserReconciler
from tests.test_controllers import FakeContext, credential_secret, user_resource


@pytest.mark.asyncio
async def test_pending_binding_precedes_tak_and_synced_status_is_idempotent() -> None:
    user = user_resource()
    context = FakeContext(user, secret=credential_secret())
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    engine = Mock(spec=UserReconciler)

    def reconcile(**_kwargs: object) -> ReconcileOutcome:
        assert context.binding is not None
        assert get_condition(context.binding, "Synced") is not None
        return ReconcileOutcome(Ownership(identifier="alpha"), FileAuthUserState("alpha"), "Synced", True)

    engine.reconcile.side_effect = reconcile
    handler.engine = engine
    try:
        await handler.reconcile(user, context.typed())
        binding = context.binding
        assert binding and binding.metadata and binding.status
        assert binding.name == "alpha" and binding.namespace == "takserver"
        assert binding.spec.user_ref.name == user.name
        assert binding.metadata.labels == {MANAGED_BY: "takoperator"}
        owner = (binding.metadata.owner_references or [])[0]
        assert owner.uid == "user-uid" and owner.kind == "User" and owner.controller
        condition = get_condition(binding, "Synced")
        assert condition and condition.status == "True" and condition.observed_generation == 1
        assert binding.status.observed_generation == 1
        assert context.binding_writes == ["create", "status", "status"]
        first = binding.model_copy(deep=True)
        await handler.reconcile(user, context.typed())
        assert context.binding == first
        assert context.binding_writes == ["create", "status", "status"]
        assert user.status and user.status["conditions"][0]["type"] == "Ready"
    finally:
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["create", "status"])
async def test_failed_binding_reservation_prevents_external_writes(failure: str) -> None:
    user = user_resource()
    context = FakeContext(user, secret=credential_secret())
    context.binding_failure = failure
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    handler.engine = Mock(spec=UserReconciler)
    try:
        with pytest.raises(ResourceConflict):
            await handler.reconcile(user, context.typed())
        handler.engine.reconcile.assert_not_called()
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_existing_tak_user_gets_binding_even_when_credential_secret_is_missing() -> None:
    user = user_resource()
    assert user.metadata and user.metadata.annotations
    user.metadata.annotations[OWNERSHIP] = Ownership(identifier="alpha").model_dump_json()
    context = FakeContext(user)
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    handler.engine = Mock(spec=UserReconciler)
    try:
        await handler.reconcile(user, context.typed())
        assert context.binding
        condition = get_condition(context.binding, "Synced")
        assert condition and condition.status == "False" and condition.reason == "CredentialSecretNotFound"
        handler.engine.reconcile.assert_not_called()
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_deleted_binding_is_recreated_but_inactive_user_does_not_churn_bindings() -> None:
    user = user_resource()
    context = FakeContext(user, secret=credential_secret())
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    engine = Mock(spec=UserReconciler)
    engine.reconcile.return_value = ReconcileOutcome(
        Ownership(identifier="alpha"), FileAuthUserState("alpha"), "Synced", True
    )
    handler.engine = engine
    try:
        await handler.reconcile(user, context.typed())
        context.binding = None  # A deleted namespaced binding requeues its User.
        await handler.reconcile(user, context.typed())
        assert context.binding and context.binding_writes.count("create") == 2
        user.spec.revoked_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
        engine.reconcile.return_value = ReconcileOutcome(Ownership(identifier="alpha"), None, "Inactive", False)
        await handler.reconcile(user, context.typed())
        writes = list(context.binding_writes)
        await handler.reconcile(user, context.typed())
        assert context.binding is None and context.binding_writes == writes
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_status_failure_retries_without_recreating_binding() -> None:
    user = user_resource()
    context = FakeContext(user)
    manager = UserBindings("takserver")
    binding = await manager.create(user, context.typed())
    context.binding_failure = "status"
    with pytest.raises(ResourceConflict):
        await manager.report(binding, context.typed(), "Synced", True)
    context.binding_failure = None
    current = await manager.get(user, context.typed())
    await manager.report(current, context.typed(), "Synced", True)
    assert context.binding
    condition = get_condition(context.binding, "Synced")
    assert condition and condition.status == "True"
    assert context.binding_writes.count("create") == 1


@pytest.mark.asyncio
async def test_status_preserves_foreign_conditions_and_transition_time() -> None:
    user = user_resource()
    context = FakeContext(user)
    manager = UserBindings("takserver")
    binding = await manager.create(user, context.typed())
    assert context.binding
    set_condition(context.binding, "Other", True, reason="Preserved")
    binding = await manager.get(user, context.typed())
    assert binding
    before = get_condition(binding, "Synced")
    await manager.report(binding, context.typed(), "WaitingForGroup", False)
    after = get_condition(binding, "Synced")
    assert before and after and before.last_transition_time == after.last_transition_time
    assert get_condition(binding, "Other") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["owner", "uid", "label", "reference"])
async def test_foreign_bindings_are_never_adopted_or_deleted(tamper: str) -> None:
    user = user_resource()
    context = FakeContext(user, secret=credential_secret())
    manager = UserBindings("takserver")
    await manager.create(user, context.typed())
    assert context.binding and context.binding.metadata
    if tamper == "owner":
        context.binding.metadata.owner_references = []
    elif tamper == "uid":
        assert context.binding.metadata.owner_references
        context.binding.metadata.owner_references[0].uid = "previous-user-uid"
    elif tamper == "label":
        context.binding.metadata.labels = {MANAGED_BY: "another-integration"}
    else:
        context.binding.spec.user_ref.name = "someone-else"
    original = context.binding.model_copy(deep=True)
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    handler.engine = Mock(spec=UserReconciler)
    try:
        await handler.reconcile(user, context.typed())
        assert user.metadata and user.metadata.annotations
        assert json.loads(user.metadata.annotations[OBSERVED])["reason"] == "BindingConflict"
        handler.engine.reconcile.assert_not_called()
        assert context.binding == original
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_failed_deprovisioning_keeps_binding_until_verified_absence() -> None:
    user = user_resource(revokedAt=datetime(2026, 9, 2, tzinfo=timezone.utc))
    assert user.metadata and user.metadata.annotations
    user.metadata.annotations[OWNERSHIP] = Ownership(identifier="alpha").model_dump_json()
    context = FakeContext(user)
    manager = UserBindings("takserver")
    binding = await manager.create(user, context.typed())
    await manager.report(binding, context.typed(), "Synced", True)
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    engine = Mock(spec=UserReconciler)
    engine.reconcile.side_effect = ReconcileError("TakUnavailable", "retry")
    handler.engine = engine
    try:
        await handler.reconcile(user, context.typed())
        assert context.binding
        condition = get_condition(context.binding, "Synced")
        assert condition and condition.status == "False"
        assert "delete" not in context.binding_writes
        engine.reconcile.side_effect = None
        engine.reconcile.return_value = ReconcileOutcome(Ownership(identifier="alpha"), None, "Inactive", False)
        await handler.reconcile(user, context.typed())
        assert context.binding is None
        assert context.binding_writes[-1] == "delete"
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_binding_deletion_failure_keeps_user_finalizer() -> None:
    user = user_resource()
    context = FakeContext(user)
    await UserBindings("takserver").create(user, context.typed())
    context.binding_failure = "delete"
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    handler.engine = Mock(spec=UserReconciler)
    try:
        with pytest.raises(ResourceConflict):
            await handler.finalize(user, context.typed())
        assert context.binding is not None
        context.binding_failure = None
        await handler.finalize(user, context.typed())
        assert context.binding is None
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_inactive_unowned_identity_releases_pending_binding() -> None:
    user = user_resource(revokedAt=datetime(2026, 9, 2, tzinfo=timezone.utc))
    context = FakeContext(user)
    await UserBindings("takserver").create(user, context.typed())
    executor = SerializedExecutor()
    handler = UserController(executor, "takserver")
    handler.engine = Mock(spec=UserReconciler)
    handler.engine.reconcile.return_value = ReconcileOutcome(None, FileAuthUserState("alpha"), "Inactive", False)
    try:
        await handler.reconcile(user, context.typed())
        assert handler.engine.reconcile.call_args.kwargs["ownership"] is None
        assert context.binding is None
    finally:
        await executor.close()
