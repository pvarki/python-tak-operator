"""External identity convergence, ownership, and partial-failure recovery."""

from dataclasses import replace
from pathlib import Path

import pytest

from takoperator.bridge import FileAuthUserState
from takoperator.reconciliation import Credential, Ownership, ReconcileError, ReconcileOutcome, UserReconciler


class MemoryStore:
    """A provider with independently persisted operations and injectable failures."""

    def __init__(self) -> None:
        self.users: dict[str, FileAuthUserState] = {}
        self.writes: list[str] = []
        self.fail_after_replace = False
        self.drop_replace = False
        self.fail_delete = False

    def get_user(self, identifier: str) -> FileAuthUserState | None:
        return self.users.get(identifier)

    def list_users(self) -> tuple[FileAuthUserState, ...]:
        return tuple(self.users.values())

    def create_password_user(self, identifier: str, password: str | None) -> FileAuthUserState:
        self.writes.append("create")
        self.users[identifier] = FileAuthUserState(identifier=identifier)
        return self.users[identifier]

    def update_password(self, identifier: str, password: str) -> FileAuthUserState:
        self.writes.append("password")
        return self.users[identifier]

    def validate_certificate(self, path: Path, expected_identifier: str) -> str:
        assert path.read_text() == "certificate fixture"
        assert path.stat().st_mode & 0o777 == 0o600
        if expected_identifier != "alice":
            raise ValueError("certificate subject mismatch")
        return "fixture-fingerprint"

    def create_certificate_user(self, path: Path, expected_identifier: str) -> FileAuthUserState:
        fingerprint = self.validate_certificate(path, expected_identifier)
        self.writes.append("certificate")
        state = self.users.get(expected_identifier, FileAuthUserState(identifier=expected_identifier))
        self.users[expected_identifier] = replace(state, fingerprint=fingerprint)
        return self.users[expected_identifier]

    def replace_existing_user_state(self, desired: FileAuthUserState) -> FileAuthUserState:
        self.writes.append("replace")
        if not self.drop_replace:
            self.users[desired.identifier] = desired
        if self.fail_after_replace:
            self.fail_after_replace = False
            raise RuntimeError("unsafe Java diagnostic containing credential")
        return self.users[desired.identifier]

    def delete_user(self, identifier: str) -> None:
        self.writes.append("delete")
        if not self.fail_delete:
            self.users.pop(identifier, None)

    def close(self) -> None:
        pass


PASSWORD = Credential(kind="password", value="ephemeral-test-value", revision="secret-uid:1")


def converge(
    engine: UserReconciler,
    groups: frozenset[str] = frozenset({"operations"}),
    ownership: Ownership | None = None,
    credential: Credential = PASSWORD,
) -> ReconcileOutcome:
    """Persist every checkpoint before the next pass, like the controller."""
    for _ in range(4):
        outcome = engine.reconcile("alice", groups, credential, ownership)
        ownership = outcome.ownership
        if not outcome.checkpoint:
            return outcome
    pytest.fail("Reconciliation did not converge")


def test_create_checkpoints_before_writing_and_second_pass_is_noop() -> None:
    store = MemoryStore()
    engine = UserReconciler(store)
    outcome = engine.reconcile("alice", frozenset({"operations"}), PASSWORD, None)
    assert outcome.checkpoint
    assert not store.writes
    outcome = engine.reconcile("alice", frozenset({"operations"}), PASSWORD, outcome.ownership)
    assert outcome.checkpoint
    assert outcome.ownership and outcome.ownership.groups == ["operations"]
    assert not store.writes
    outcome = converge(engine, ownership=outcome.ownership)
    assert outcome.ready
    assert outcome.observed and outcome.observed.groups == frozenset({"operations"})
    assert store.writes == ["create", "replace"]
    assert converge(engine, ownership=outcome.ownership).ready
    assert store.writes == ["create", "replace"]


def test_never_adopt_an_existing_unowned_identity() -> None:
    store = MemoryStore()
    store.users["alice"] = FileAuthUserState(identifier="alice")
    with pytest.raises(ReconcileError, match="already exists") as error:
        converge(UserReconciler(store))
    assert error.value.reason == "IdentityConflict"
    assert not store.writes


def test_preserve_foreign_groups_directions_and_server_privileges() -> None:
    store = MemoryStore()
    original = FileAuthUserState(
        identifier="alice",
        groups=frozenset({"foreign", "old"}),
        in_groups=frozenset({"incoming"}),
        out_groups=frozenset({"outgoing"}),
        explicit_server_role="ROLE_ADMIN",
        effective_server_role="ROLE_ADMIN",
    )
    store.users["alice"] = original
    outcome = converge(
        UserReconciler(store),
        groups=frozenset({"operations", "foreign"}),
        ownership=Ownership(identifier="alice", groups=["old"], credential_revision=PASSWORD.revision),
    )
    assert outcome.observed == replace(original, groups=frozenset({"foreign", "operations"}))
    assert outcome.ownership and outcome.ownership.groups == ["operations"]
    outcome = converge(UserReconciler(store), groups=frozenset(), ownership=outcome.ownership)
    assert outcome.observed == replace(original, groups=frozenset({"foreign"}))


def test_password_rotation_is_versioned_and_not_exposed() -> None:
    store = MemoryStore()
    engine = UserReconciler(store)
    outcome = converge(engine)
    changed = replace(PASSWORD, value="different-ephemeral-value", revision="secret-uid:2")
    outcome = converge(engine, ownership=outcome.ownership, credential=changed)
    assert store.writes.count("password") == 1
    assert outcome.ownership and outcome.ownership.credential_revision == "secret-uid:2"
    converge(engine, ownership=outcome.ownership, credential=changed)
    assert store.writes.count("password") == 1
    assert changed.value not in repr(changed)
    assert changed.value not in repr(outcome)


def test_certificate_create_and_drift_repair() -> None:
    store = MemoryStore()
    engine = UserReconciler(store)
    credential = Credential(kind="certificate", value="certificate fixture", revision="cert:1")
    outcome = converge(engine, credential=credential)
    assert outcome.observed and outcome.observed.fingerprint == "fixture-fingerprint"
    converge(engine, ownership=outcome.ownership, credential=credential)
    assert store.writes.count("certificate") == 1
    store.users["alice"] = replace(store.users["alice"], fingerprint="external-change")
    outcome = converge(engine, ownership=outcome.ownership, credential=credential)
    assert outcome.ready
    assert store.writes.count("certificate") == 2


def test_partial_success_reports_observed_state_and_retries_without_rollback() -> None:
    store = MemoryStore()
    store.users["alice"] = FileAuthUserState(identifier="alice")
    ownership = Ownership(identifier="alice", groups=["operations"], credential_revision=PASSWORD.revision)
    store.fail_after_replace = True
    engine = UserReconciler(store)
    with pytest.raises(ReconcileError) as error:
        converge(engine, ownership=ownership)
    assert error.value.reason == "TakUnavailable"
    assert error.value.observed and error.value.observed.groups == frozenset({"operations"})
    assert "unsafe Java diagnostic" not in str(error.value)
    assert converge(engine, ownership=ownership).ready
    assert store.writes == ["replace"]


def test_verification_failure_is_not_reported_ready() -> None:
    store = MemoryStore()
    store.drop_replace = True
    with pytest.raises(ReconcileError) as error:
        converge(UserReconciler(store))
    assert error.value.reason == "VerificationFailed"
    assert error.value.observed and not error.value.observed.groups


def test_identity_rename_cannot_delete_or_replace_old_user() -> None:
    store = MemoryStore()
    with pytest.raises(ReconcileError) as error:
        UserReconciler(store).reconcile("bob", frozenset(), PASSWORD, Ownership(identifier="alice"))
    assert error.value.reason == "ImmutableIdentifier"
    assert not store.writes


def test_inactive_user_removes_owned_identity_without_requiring_secret() -> None:
    store = MemoryStore()
    engine = UserReconciler(store)
    outcome = converge(engine)
    outcome = engine.reconcile("alice", frozenset(), None, outcome.ownership, active=False)
    assert outcome.reason == "Inactive"
    assert not store.users
    assert outcome.ownership and outcome.ownership.credential_revision is None
    # No ownership means no deletion, even when the same callsign exists externally.
    store.users["alice"] = FileAuthUserState(identifier="alice")
    store.writes.clear()
    engine.reconcile("alice", frozenset(), None, None, active=False)
    assert not store.writes


def test_finalizer_delete_retain_and_failed_verification() -> None:
    store = MemoryStore()
    engine = UserReconciler(store)
    outcome = converge(engine)
    engine.finalize(outcome.ownership, retain=True)
    assert "alice" in store.users
    store.fail_delete = True
    with pytest.raises(ReconcileError) as error:
        engine.finalize(outcome.ownership)
    assert error.value.reason == "VerificationFailed"
    store.fail_delete = False
    engine.finalize(outcome.ownership)
    engine.finalize(outcome.ownership)
    engine.finalize(None)
    assert not store.users


def test_missing_credentials_does_not_claim_identity() -> None:
    store = MemoryStore()
    with pytest.raises(ReconcileError) as error:
        UserReconciler(store).reconcile("alice", frozenset(), None, None)
    assert error.value.reason == "MissingCredential"
    assert not store.writes
