"""Consume the platform operator's resources without installing its CRDs."""

from datetime import datetime
from typing import Any, ClassVar

from cloudcoil.pydantic import BaseModel
from cloudcoil.resources import Resource
from pydantic import ConfigDict, Field


class ObjectRef(BaseModel):
    """Reference to a cluster-scoped platform resource."""

    name: str = Field(min_length=1)


class UserSpec(BaseModel):
    """Fields consumed by TAK; preserve additional platform fields."""

    model_config = ConfigDict(extra="allow")
    callsign: str = Field(min_length=1)
    public_key: str | None = Field(default=None, alias="publicKey")
    revoked_at: datetime | None = Field(default=None, alias="revokedAt")
    approved_at: datetime | None = Field(default=None, alias="approvedAt")
    group_refs: list[ObjectRef] = Field(default_factory=list, alias="groupRefs")
    role_refs: list[ObjectRef] = Field(default_factory=list, alias="roleRefs")


class GroupSpec(BaseModel):
    """The platform group's TAK routing name."""

    model_config = ConfigDict(extra="allow")
    name: str = Field(min_length=1)
    role_refs: list[ObjectRef] = Field(default_factory=list, alias="roleRefs")


class RoleSpec(BaseModel):
    """Operational role; never a TAK server authentication privilege."""

    model_config = ConfigDict(extra="allow")
    name: str = Field(min_length=1)


class PlatformResource(Resource):
    """Preserve the owning platform operator's specification and status."""

    model_config = ConfigDict(extra="allow")
    api_version: Any | None = Field(default="platform.opendefence.fi/v1alpha1", alias="apiVersion")
    status: dict[str, Any] | None = None


class User(PlatformResource):
    """Externally installed cluster-scoped User."""

    # This consumer has no status permissions. Runtime discovery still knows the
    # installed CRD's real endpoints; this metadata controls offline RBAC only.
    __cloudcoil_api__: ClassVar[dict[str, Any]] = {"plural": "users", "scope": "Cluster", "status": False}
    kind: Any | None = "User"
    spec: UserSpec


class Group(PlatformResource):
    """Externally installed cluster-scoped Group."""

    __cloudcoil_api__: ClassVar[dict[str, Any]] = {"plural": "groups", "scope": "Cluster", "status": False}
    kind: Any | None = "Group"
    spec: GroupSpec


class Role(PlatformResource):
    """Externally installed cluster-scoped operational Role."""

    __cloudcoil_api__: ClassVar[dict[str, Any]] = {"plural": "roles", "scope": "Cluster", "status": False}
    kind: Any | None = "Role"
    spec: RoleSpec
