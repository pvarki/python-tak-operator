"""Cloudcoil adapters for the serialized TAK reconciliation engine."""

import asyncio
import base64
import binascii
import json
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from functools import partial
from typing import Any

from cloudcoil.application import Application
from cloudcoil.controller import Context, Controller, Request, ResourceKey, Result
from cloudcoil.errors import ResourceConflict, ResourceNotFound
from cloudcoil.models.kubernetes.core.v1 import Secret
from pydantic import ValidationError

from takoperator.models import Group, PlatformResource, Role, User
from takoperator.reconciliation import Credential, Ownership, ReconcileError, UserReconciler

ANNOTATION_PREFIX = "tak.opendefence.fi/"
CREDENTIAL_SECRET = f"{ANNOTATION_PREFIX}credential-secret"
OWNERSHIP = f"{ANNOTATION_PREFIX}ownership"
OBSERVED = f"{ANNOTATION_PREFIX}observed"
DELETION_POLICY = f"{ANNOTATION_PREFIX}deletion-policy"
FINALIZER = f"{ANNOTATION_PREFIX}file-auth-user"


class SerializedExecutor:
    """Keep JNI on one thread and drain an in-flight call before cancellation."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tak-jni")
        self._lock = asyncio.Lock()
        self._closed = False

    async def run[T](self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Cancel waiting callers freely; never abandon a running JVM mutation."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("TAK executor is closed")
            future = asyncio.get_running_loop().run_in_executor(self._pool, partial(function, *args, **kwargs))
            cancelled = False
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    if not cancelled:
                        raise
            if cancelled:
                # Consume a concurrent failure before propagating cancellation.
                future.exception()
                raise asyncio.CancelledError
            return future.result()

    async def close(self) -> None:
        """Wait for callers, then stop the idle worker without blocking the loop."""
        async with self._lock:
            self._closed = True
            self._pool.shutdown(wait=True)


def annotations(resource: PlatformResource) -> dict[str, str]:
    """Read optional annotations without modifying the resource."""
    return resource.metadata.annotations or {} if resource.metadata else {}


def observe(resource: PlatformResource, reason: str, ready: bool, **details: Any) -> None:
    """Publish TAK's observation without taking ownership of platform status."""
    if resource.metadata is None:
        raise ValueError("A fetched platform resource needs metadata")
    payload = {"reason": reason, "ready": ready, "observedGeneration": resource.metadata.generation, **details}
    resource.metadata.annotations = {
        **annotations(resource),
        OBSERVED: json.dumps(payload, sort_keys=True, separators=(",", ":")),
    }


def ownership_for(user: User) -> Ownership | None:
    """Invalid ownership must stop work, especially destructive cleanup."""
    encoded = annotations(user).get(OWNERSHIP)
    return Ownership.model_validate_json(encoded) if encoded else None


def credential_from_secret(secret: Secret) -> Credential:
    """Decode exactly one supported credential without retaining it in status."""
    data = secret.data or {}
    keys = [key for key in ("password", "tls.crt") if key in data]
    if len(keys) != 1 or secret.metadata is None or not secret.metadata.uid or not secret.resource_version:
        raise ValueError("Credential Secret needs exactly one of password or tls.crt, and server identity metadata")
    key = keys[0]
    try:
        value = base64.b64decode(data[key], validate=True).decode("utf-8")
    except ValueError, UnicodeDecodeError, binascii.Error:
        raise ValueError("Credential Secret data is not valid base64-encoded UTF-8") from None
    if not value:
        raise ValueError("Credential Secret data is empty")
    return Credential(
        kind="password" if key == "password" else "certificate",
        value=value,
        revision=f"{secret.metadata.uid}:{secret.resource_version}",
    )


def referenced_users(users: Iterable[User], field: str, name: str | None) -> list[ResourceKey]:
    """Map a dependency event to primary keys without API calls."""
    result = []
    for user in users:
        refs = user.spec.group_refs if field == "groups" else user.spec.role_refs
        if user.name and any(ref.name == name for ref in refs):
            result.append(ResourceKey(user.name))
    return result


class UserController:
    """Resolve Kubernetes inputs and persist engine checkpoints before mutations."""

    def __init__(self, executor: SerializedExecutor, namespace: str) -> None:
        self.executor = executor
        self.namespace = namespace
        self.engine: UserReconciler | None = None

    async def _desired_groups(self, user: User, ctx: Context[User]) -> tuple[frozenset[str], list[str], bool]:
        groups: set[str] = set()
        missing: list[str] = []
        operational_roles = bool(user.spec.role_refs)
        for ref in user.spec.group_refs:
            try:
                group = await ctx.get(Group, ref.name)
            except ResourceNotFound:
                missing.append(ref.name)
                continue
            if group.metadata and group.metadata.deletion_timestamp:
                missing.append(ref.name)
                continue
            groups.add(group.spec.name)
            operational_roles = operational_roles or bool(group.spec.role_refs)
        return frozenset(groups), missing, operational_roles

    async def _check_identity(self, user: User, ctx: Context[User], *, active: bool) -> bool:
        if not user.metadata or not user.name:
            raise ValueError("A fetched User needs metadata and name")
        if active:
            client = await ctx.client(User)
            async for other in await client.list():
                if (
                    other.metadata
                    and other.metadata.uid != user.metadata.uid
                    and other.spec.callsign == user.spec.callsign
                ):
                    return False
        current = await ctx.get(User, user.name)
        if (
            not current.metadata
            or current.metadata.uid != user.metadata.uid
            or current.resource_version != user.resource_version
            or current.metadata.deletion_timestamp is not None
        ):
            raise ResourceConflict("User changed while resolving TAK inputs", status_code=409)
        return True

    async def _credential(self, user: User, ctx: Context[User]) -> Credential:
        secret_name = annotations(user).get(CREDENTIAL_SECRET)
        if not secret_name or "/" in secret_name:
            raise ReconcileError("CredentialSecretRequired", "Reference a credential Secret in the operator namespace")
        try:
            secret = await ctx.get(Secret, secret_name, namespace=self.namespace)
            return credential_from_secret(secret)
        except ResourceNotFound:
            raise ReconcileError("CredentialSecretNotFound", "The referenced credential Secret is absent") from None
        except ValueError:
            raise ReconcileError("InvalidCredentialSecret", "The credential Secret is invalid") from None

    async def reconcile(self, user: User, ctx: Context[User]) -> Result:
        """Converge one identity; every mutation uses freshly resolved inputs."""
        if self.engine is None:
            raise RuntimeError("TAK is not connected")
        try:
            ownership = ownership_for(user)
        except ValidationError:
            observe(user, "InvalidOwnership", False)
            return Result(resource=user, requeue_after=60)
        if ownership and ownership.identifier != user.spec.callsign:
            observe(user, "ImmutableIdentifier", False, identifier=ownership.identifier)
            return Result(resource=user, requeue_after=60)
        active = user.spec.approved_at is not None and user.spec.revoked_at is None
        groups: frozenset[str] = frozenset()
        missing: list[str] = []
        operational_roles = False
        if active:
            groups, missing, operational_roles = await self._desired_groups(user, ctx)
        try:
            credential = await self._credential(user, ctx) if active else None
            if not await self._check_identity(user, ctx, active=active):
                raise ReconcileError("DuplicateIdentifier", "Multiple platform Users have the same TAK identifier")
            outcome = await self.executor.run(
                self.engine.reconcile,
                identifier=user.spec.callsign,
                groups=groups,
                credential=credential,
                ownership=ownership,
                active=active,
            )
        except ReconcileError as error:
            observe(user, error.reason, False, state=_state(error.observed))
            return Result(resource=user, requeue_after=10)
        if user.metadata is None:
            raise ValueError("A fetched User needs metadata")
        owned_annotations = dict(annotations(user))
        if outcome.ownership is not None:
            owned_annotations[OWNERSHIP] = outcome.ownership.model_dump_json()
        else:
            owned_annotations.pop(OWNERSHIP, None)
        user.metadata.annotations = owned_annotations
        observe(
            user,
            "GroupNotFound" if missing and not outcome.checkpoint else outcome.reason,
            outcome.ready and not missing,
            state=_state(outcome.observed),
            missingGroups=missing,
            operationalRoles="Unsupported" if operational_roles else "NotRequested",
        )
        return Result(resource=user, requeue_after=1 if outcome.checkpoint else 60)

    async def finalize(self, user: User) -> None:
        """Delete only the identity recorded before provisioning this resource."""
        if self.engine is None:
            raise RuntimeError("TAK is not connected")
        policy = annotations(user).get(DELETION_POLICY, "Delete")
        if policy not in ("Delete", "Retain"):
            raise ReconcileError("InvalidDeletionPolicy", "Deletion policy must be Delete or Retain")
        await self.executor.run(
            self.engine.finalize,
            ownership_for(user),
            retain=policy == "Retain",
        )


def _state(observed: Any) -> dict[str, Any] | None:
    if observed is None:
        return None
    return {key: sorted(value) if isinstance(value, frozenset) else value for key, value in asdict(observed).items()}


def register_controllers(app: Application, handler: UserController) -> Controller[User]:
    """Watch platform resources while leaving their CRDs and status to their owner."""
    users = app.controller(User, report_status=False, status_updates=False, events=False, namespace=handler.namespace)
    users.reconcile(every=60)(handler.reconcile)
    users.finalize(FINALIZER)(handler.finalize)

    @users.watch(Group)
    def group_changed(group: Group) -> list[ResourceKey]:
        return referenced_users(users.cached(User).list(), "groups", group.name) if users.ready else []

    @users.watch(Role)
    def role_changed(role: Role) -> list[ResourceKey]:
        if not users.ready:
            return []
        # Group roles also affect the operational-role observation.
        return [ResourceKey(user.name) for user in users.cached(User).list() if user.name]

    async def secret_changed(request: Request[Secret]) -> None:
        # A namespaced primary keeps Secret permissions local. Cloudcoil 0.8.0
        # expands child watches of cluster-scoped parents across all namespaces.
        # Low-level Request also delivers Secret deletions (resource is None).
        if users.ready:
            for user in users.cached(User).list():
                if user.name and annotations(user).get(CREDENTIAL_SECRET) == request.key.name:
                    users.enqueue(ResourceKey(user.name))

    app.include(
        Controller(
            Secret,
            reconcile=secret_changed,
            namespace=handler.namespace,
            report_status=False,
            status_updates=False,
            events=False,
        )
    )

    groups = app.controller(Group, report_status=False, status_updates=False, events=False)

    @groups.reconcile()
    async def reconcile_group(group: Group) -> Group:
        observe(group, "MembershipsManagedByUsers", True, name=group.spec.name, materialization="MembershipEdges")
        return group

    roles = app.controller(Role, report_status=False, status_updates=False, events=False)

    @roles.reconcile()
    async def reconcile_role(role: Role) -> Role:
        observe(role, "UnsupportedOperationalRole", False, name=role.spec.name)
        return role

    return users
