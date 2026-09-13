"""Converge external identities with durable, explicit ownership checkpoints."""

from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel, Field

from takoperator.bridge import FileAuthUserState, TakFileAuthStore


@dataclass(frozen=True)
class Credential:
    """Secret contents exist only for the duration of a reconciliation."""

    kind: Literal["password", "certificate"]
    value: str = field(repr=False)
    revision: str


class Ownership(BaseModel):
    """Write-ahead record stored on the Kubernetes object before TAK writes."""

    identifier: str
    groups: list[str] = Field(default_factory=list)
    credential_revision: str | None = None


@dataclass(frozen=True)
class ReconcileOutcome:
    """Observed external state and the next durable ownership record."""

    ownership: Ownership | None
    observed: FileAuthUserState | None
    reason: str
    ready: bool
    checkpoint: bool = False


class ReconcileError(Exception):
    """A credential-free error safe to publish on the resource."""

    def __init__(self, reason: str, message: str, observed: FileAuthUserState | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.observed = observed


class UserReconciler:
    """Own identity lifecycle and User.groupRefs edges, preserving foreign edges.

    The caller must persist checkpoint outcomes before calling again, and serialize
    calls through a single leader. A Python lock cannot provide distributed fencing.
    """

    def __init__(self, store: TakFileAuthStore) -> None:
        self.store = store

    def reconcile(
        self,
        identifier: str,
        groups: frozenset[str],
        credential: Credential | None,
        ownership: Ownership | None,
        active: bool = True,
    ) -> ReconcileOutcome:
        """Read, checkpoint intent, mutate, then verify persisted state."""
        if ownership is not None and ownership.identifier != identifier:
            raise ReconcileError("ImmutableIdentifier", "Create a new User to change an owned TAK identifier")
        try:
            return self._reconcile(identifier, groups, credential, ownership, active)
        except ReconcileError:
            raise
        except Exception:
            # A failed remote operation may already have persisted. Never infer rollback
            # or expose Java exception messages that can contain credential arguments.
            try:
                observed = self.store.get_user(identifier)
            except Exception:
                observed = None
            raise ReconcileError(
                "TakUnavailable", "TAK operation failed; reconciliation will retry", observed
            ) from None

    def _reconcile(
        self,
        identifier: str,
        groups: frozenset[str],
        credential: Credential | None,
        ownership: Ownership | None,
        active: bool,
    ) -> ReconcileOutcome:
        current = self.store.get_user(identifier)
        if not active:
            if ownership is not None:
                self.finalize(ownership)
                ownership = Ownership(identifier=identifier)
                current = None
            return ReconcileOutcome(ownership, current, "Inactive", False)
        if credential is None:
            raise ReconcileError("MissingCredential", "A credential Secret is required", current)
        if ownership is None:
            if current is not None:
                raise ReconcileError(
                    "IdentityConflict", "TAK identifier already exists and is not owned by this User", current
                )
            return ReconcileOutcome(Ownership(identifier=identifier), None, "OwnershipPending", False, checkpoint=True)

        # Only claim edges that this resource adds. Existing manual memberships stay
        # manual, including when they happen to match groupRefs on the first pass.
        owned = frozenset(ownership.groups)
        additions = groups - (current.groups if current else frozenset())
        intent = owned | additions
        if intent != owned:
            pending = ownership.model_copy(update={"groups": sorted(intent)})
            return ReconcileOutcome(pending, current, "MembershipPending", False, checkpoint=True)

        current = self._credentials(identifier, credential, ownership, current)
        desired = replace(current, groups=(current.groups - owned) | groups)
        if desired != current:
            self.store.replace_existing_user_state(desired)
        observed = self.store.get_user(identifier)
        if observed != desired:
            raise ReconcileError(
                "VerificationFailed", "TAK state differs after the write; reconciliation will retry", observed
            )
        updated = Ownership(
            identifier=identifier,
            groups=sorted(intent & groups),
            credential_revision=credential.revision,
        )
        return ReconcileOutcome(updated, observed, "Synced", True)

    def _credentials(
        self,
        identifier: str,
        credential: Credential,
        ownership: Ownership,
        current: FileAuthUserState | None,
    ) -> FileAuthUserState:
        if credential.kind == "certificate":
            with TemporaryDirectory(prefix="tak-credential-") as directory:
                certificate_path = Path(directory) / "certificate.pem"
                certificate_path.write_text(credential.value, encoding="utf-8")
                certificate_path.chmod(0o600)
                fingerprint = self.store.validate_certificate(certificate_path, identifier)
                if current is None or current.fingerprint != fingerprint:
                    self.store.create_certificate_user(certificate_path, identifier)
        elif current is None:
            self.store.create_password_user(identifier, credential.value)
        elif ownership.credential_revision != credential.revision:
            self.store.update_password(identifier, credential.value)
        observed = self.store.get_user(identifier)
        if observed is None:
            raise ReconcileError("VerificationFailed", "TAK identity is absent after credential reconciliation")
        return observed

    def finalize(self, ownership: Ownership | None, retain: bool = False) -> None:
        """Delete only the recorded identity; keep the finalizer on failure."""
        if ownership is None or retain:
            return
        try:
            self.store.delete_user(ownership.identifier)
            observed = self.store.get_user(ownership.identifier)
        except Exception:
            raise ReconcileError("TakUnavailable", "TAK deletion failed; cleanup will retry") from None
        if observed is not None:
            raise ReconcileError("VerificationFailed", "TAK identity remains after deletion", observed)
