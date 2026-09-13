"""Typed file-auth boundary for TAK's Ignite-backed Java user manager.

Java objects and password hashes never leave this module. Remote writes may
partially succeed, so every mutation is followed by an authoritative read.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

_MUTATION_LOCK = RLock()


@dataclass(frozen=True)
class FileAuthUserState:
    """Observed account state; operational CoT roles are a separate concept."""

    identifier: str
    fingerprint: str | None = None
    explicit_server_role: str | None = None
    effective_server_role: str = "ROLE_ANONYMOUS"
    groups: frozenset[str] = frozenset()
    in_groups: frozenset[str] = frozenset()
    out_groups: frozenset[str] = frozenset()


class TakFileAuthStore(Protocol):
    """Synchronous store; controllers run this on their serialized worker."""

    def get_user(self, identifier: str) -> FileAuthUserState | None: ...
    def list_users(self) -> tuple[FileAuthUserState, ...]: ...
    def create_password_user(self, identifier: str, password: str | None) -> FileAuthUserState: ...
    def update_password(self, identifier: str, password: str) -> FileAuthUserState: ...
    def validate_certificate(self, path: Path, expected_identifier: str) -> str: ...
    def create_certificate_user(self, path: Path, expected_identifier: str) -> FileAuthUserState: ...
    def replace_existing_user_state(self, desired: FileAuthUserState) -> FileAuthUserState: ...
    def delete_user(self, identifier: str) -> None: ...


class TakMutationError(RuntimeError):
    """A write failed; observed state may contain changes already persisted."""

    def __init__(self, identifier: str, observed: FileAuthUserState | None, *, observation_available: bool) -> None:
        super().__init__(f"TAK mutation failed for {identifier}; reconcile from observed state")
        self.observed = observed
        self.observation_available = observation_available


class JniFileAuthStore:
    """Direct FileUserManagementInterface adapter with injectable Java classes."""

    def __init__(self, manager: Any, *, user_class: Any, role_class: Any, boolean_class: Any, ssl_helper: Any) -> None:
        self._manager = manager
        self._user_class = user_class
        self._role_class = role_class
        self._boolean_class = boolean_class
        self._ssl_helper = ssl_helper
        self._pid = os.getpid()

    def _check_process(self) -> None:
        if os.getpid() != self._pid:
            raise RuntimeError("TAK JVM cannot be used after forking")

    def _snapshot(self, user: Any) -> FileAuthUserState:
        role = user.getRole()
        identifier = str(user.getIdentifier())
        return FileAuthUserState(
            identifier=identifier,
            fingerprint=user.getFingerprint(),
            explicit_server_role=str(role.value()) if role is not None else None,
            effective_server_role=str(self._manager.getUserRole(identifier).value()),
            groups=frozenset(user.getGroupList().toArray()),
            in_groups=frozenset(user.getGroupListIN().toArray()),
            out_groups=frozenset(user.getGroupListOUT().toArray()),
        )

    def get_user(self, identifier: str) -> FileAuthUserState | None:
        """Read all three membership lists, including directional memberships."""
        self._check_process()
        with _MUTATION_LOCK:
            user = self._manager.getFirstUser(identifier)
            return None if user is None else self._snapshot(user)

    def list_users(self) -> tuple[FileAuthUserState, ...]:
        """Read complete snapshots; getUsersWithGroups omits IN/OUT edges."""
        self._check_process()
        with _MUTATION_LOCK:
            return tuple(
                sorted(
                    (self._snapshot(u) for u in self._manager.getAllUsers().toArray()), key=lambda user: user.identifier
                )
            )

    def _write(self, identifier: str, operation: Callable[[], Any]) -> FileAuthUserState | None:
        try:
            operation()
        except Exception as exc:
            try:
                observed = self.get_user(identifier)
            except Exception:
                raise TakMutationError(identifier, None, observation_available=False) from exc
            raise TakMutationError(identifier, observed, observation_available=True) from exc
        return self.get_user(identifier)

    def _require_user(self, identifier: str, state: FileAuthUserState | None) -> FileAuthUserState:
        if state is None:
            raise RuntimeError(f"TAK user {identifier} is absent after write")
        return state

    def create_password_user(self, identifier: str, password: str | None) -> FileAuthUserState:
        """Create only the account; preserve any existing credentials on retries."""
        self._check_process()
        with _MUTATION_LOCK:
            existing = self.get_user(identifier)
            if existing is not None:
                return existing
            return self._require_user(
                identifier, self._write(identifier, lambda: self._manager.addOrUpdateUser(identifier, password, False))
            )

    def update_password(self, identifier: str, password: str) -> FileAuthUserState:
        """Update a credential without exposing the stored hash to controllers."""
        self._check_process()
        with _MUTATION_LOCK:
            if self.get_user(identifier) is None:
                raise ValueError(f"TAK user {identifier} does not exist")
            return self._require_user(
                identifier, self._write(identifier, lambda: self._manager.addOrUpdateUser(identifier, password, False))
            )

    def _certificate(self, path: Path, expected_identifier: str) -> tuple[Any, str]:
        certificate = self._ssl_helper.getCertificate(str(path.resolve()))
        identifier = str(self._ssl_helper.getCertificateUserName(certificate))
        if identifier != expected_identifier:
            raise ValueError("Certificate identity does not match the requested TAK identifier")
        fingerprint = str(self._ssl_helper.loadCertFingerprintForEndUser(str(path.resolve())))
        return certificate, fingerprint

    def validate_certificate(self, path: Path, expected_identifier: str) -> str:
        """Use TAK's identity rules before any account mutation."""
        self._check_process()
        with _MUTATION_LOCK:
            return self._certificate(path, expected_identifier)[1]

    def create_certificate_user(self, path: Path, expected_identifier: str) -> FileAuthUserState:
        """Register a certificate, checking identity before the remote call."""
        self._check_process()
        with _MUTATION_LOCK:
            certificate, fingerprint = self._certificate(path, expected_identifier)
            current = self.get_user(expected_identifier)
            if current is not None and current.fingerprint == fingerprint:
                return current
            state = self._require_user(
                expected_identifier,
                self._write(expected_identifier, lambda: self._manager.addOrUpdateUserFromCertificate(certificate)),
            )
            if state.fingerprint != fingerprint:
                raise RuntimeError("TAK certificate fingerprint differs after registration")
            return state

    def _copy_user(self, current: Any) -> Any:
        """Copy every JAXB User field, including credentials kept inside JNI."""
        user = self._user_class()
        user.setIdentifier(current.getIdentifier())
        user.setFingerprint(current.getFingerprint())
        user.setPassword(current.getPassword())
        hashed = current.isPasswordHashed()
        if hashed is not None:
            value = hashed.booleanValue() if hasattr(hashed, "booleanValue") else bool(hashed)
            user.setPasswordHashed(self._boolean_class(value))
        user.setRole(current.getRole())
        for getter in ("getGroupList", "getGroupListIN", "getGroupListOUT"):
            for group in getattr(current, getter)().toArray():
                getattr(user, getter)().add(group)
        return user

    def replace_existing_user_state(self, desired: FileAuthUserState) -> FileAuthUserState:
        """Replace managed fields using fresh old/new objects and one RPC."""
        self._check_process()
        with _MUTATION_LOCK:
            current = self._manager.getFirstUser(desired.identifier)
            if current is None:
                raise ValueError(f"TAK user {desired.identifier} does not exist")
            if self._snapshot(current) == desired:
                return desired
            old = self._copy_user(current)
            replacement = self._copy_user(current)
            replacement.setFingerprint(desired.fingerprint)
            replacement.setRole(
                None
                if desired.explicit_server_role is None
                else self._role_class.fromValue(desired.explicit_server_role)
            )
            for getter, groups in (
                ("getGroupList", desired.groups),
                ("getGroupListIN", desired.in_groups),
                ("getGroupListOUT", desired.out_groups),
            ):
                target = getattr(replacement, getter)()
                target.clear()
                for group in sorted(groups):
                    target.add(group)
            # The copied password is already the current server representation.
            # Passing True prevents hashing that hash again when other fields change.
            return self._require_user(
                desired.identifier,
                self._write(desired.identifier, lambda: self._manager.addOrUpdateUser(replacement, True, old)),
            )

    def delete_user(self, identifier: str) -> None:
        """Remove the account and verify absence before finalizer removal."""
        self._check_process()
        with _MUTATION_LOCK:
            if self.get_user(identifier) is None:
                return
            if self._write(identifier, lambda: self._manager.removeUser(identifier)) is not None:
                raise RuntimeError(f"TAK user {identifier} remains after deletion")
