"""Application composition; JVM connections belong to the elected leader."""

import os
from collections.abc import AsyncIterator

from cloudcoil.application import Application, RBACRule
from cloudcoil.controller import HealthServer
from cloudcoil.models.kubernetes.core.v1 import Secret

from takoperator.controllers import SerializedExecutor, UserController, register_controllers
from takoperator.models import UserBinding
from takoperator.reconciliation import UserReconciler
from takoperator.runtime import TakJvmRuntime, TakJvmSettings


def create_application(namespace: str | None = None) -> Application:
    """Build the operator without connecting to Kubernetes or starting a JVM."""
    namespace = namespace or os.environ.get("CLOUDCOIL_NAMESPACE", "tak-operator-system")
    app = Application(
        "takoperator",
        namespace=namespace,
        leader_election=True,
        health=HealthServer(host="0.0.0.0", port=8080),  # nosec B104: Kubernetes probes need the Pod interface
        rules=(
            RBACRule(Secret, ("get", "list", "watch"), plural="secrets", scope="Namespaced"),
            RBACRule(UserBinding, ("get", "list", "watch", "create", "delete")),
            RBACRule(UserBinding, ("update",), subresources=("status",)),
        ),
    )
    executor = SerializedExecutor()
    handler = UserController(executor, namespace)
    register_controllers(app, handler)

    @app.lifespan(scope="leader")
    async def leadership() -> AsyncIterator[None]:
        runtime = TakJvmRuntime(TakJvmSettings.from_env())
        try:
            handler.engine = UserReconciler(await executor.run(runtime.start))
            yield
        finally:
            # Cloudcoil drains workers before this hook and keeps lease renewal
            # running during normal shutdown. JNI work is drained even on cancel.
            handler.engine = None
            await executor.run(runtime.close)

    @app.lifespan()
    async def process() -> AsyncIterator[None]:
        try:
            yield
        finally:
            await executor.close()

    return app
