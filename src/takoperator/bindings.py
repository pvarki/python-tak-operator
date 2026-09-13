"""Maintain namespaced integration records without adopting foreign bindings."""

from cloudcoil.apimachinery import ObjectMeta, OwnerReference
from cloudcoil.controller import Context, set_condition, update_status
from cloudcoil.errors import ResourceNotFound

from takoperator.models import ObjectRef, User, UserBinding, UserBindingSpec
from takoperator.reconciliation import ReconcileError

MANAGED_BY = "app.kubernetes.io/managed-by"


class UserBindings:
    """Reserve the User before TAK writes and release it after verified removal."""

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace

    async def get(self, user: User, ctx: Context[User]) -> UserBinding | None:
        """Read the deterministic binding and require this User's UID ownership."""
        if not user.name or not user.metadata or not user.metadata.uid:
            raise ValueError("A fetched User needs a name and UID")
        try:
            binding = await ctx.get(UserBinding, user.name, namespace=self.namespace)
        except ResourceNotFound:
            return None
        metadata = binding.metadata
        owned = metadata and (metadata.labels or {}).get(MANAGED_BY) == "takoperator"
        owner = metadata and any(
            ref.api_version == user.api_version
            and ref.kind == "User"
            and ref.name == user.name
            and ref.uid == user.metadata.uid
            and ref.controller is True
            for ref in metadata.owner_references or []
        )
        if not owned or not owner or binding.spec.user_ref.name != user.name:
            raise ReconcileError("BindingConflict", "The UserBinding name is owned by another resource or integration")
        if metadata and metadata.deletion_timestamp:
            raise ReconcileError("BindingDeleting", "Wait for the previous UserBinding to finish deleting")
        return binding

    async def create(self, user: User, ctx: Context[User]) -> UserBinding:
        """Create a pending record; a create race retries through ownership checks."""
        if not user.name or not user.metadata or not user.metadata.uid:
            raise ValueError("A fetched User needs a name and UID")
        binding = UserBinding(
            metadata=ObjectMeta(
                name=user.name,
                namespace=self.namespace,
                labels={MANAGED_BY: "takoperator"},
                owner_references=[
                    OwnerReference(
                        api_version=str(user.api_version),
                        kind="User",
                        name=user.name,
                        uid=user.metadata.uid,
                        controller=True,
                        block_owner_deletion=False,
                    )
                ],
            ),
            spec=UserBindingSpec(user_ref=ObjectRef(name=user.name)),
        )
        client = await ctx.client(UserBinding)
        binding = await client.create(binding)
        await self.report(binding, ctx, "Provisioning", False)
        return binding

    async def report(self, binding: UserBinding | None, ctx: Context[User], reason: str, ready: bool) -> None:
        """Only write changed status; preserve other conditions and transition time."""
        if binding is None:
            return
        before = binding.status
        set_condition(binding, "Synced", ready, reason=reason)
        update_status(binding, observed_generation=binding.metadata.generation if binding.metadata else None)
        if binding.status != before:
            client = await ctx.client(UserBinding)
            saved = await client.update_status(binding)
            binding.metadata = saved.metadata
            binding.status = saved.status

    async def delete(self, binding: UserBinding | None, ctx: Context[User]) -> None:
        """Protect a concurrent same-name replacement with UID/version preconditions."""
        if binding is None:
            return
        if not binding.name or not binding.metadata or not binding.metadata.uid or not binding.resource_version:
            raise ValueError("A fetched UserBinding needs a name, UID and resourceVersion")
        client = await ctx.client(UserBinding)
        try:
            await client.delete(
                binding.name,
                namespace=self.namespace,
                uid=binding.metadata.uid,
                resource_version=binding.resource_version,
            )
        except ResourceNotFound:
            pass
