"""Guard shared-volume cutover ordering and Ignite bootstrap routing."""

import asyncio
import os
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FAKE_KUBECTL = """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$TAK_TEST_LOG"
case "$*" in
  *--dry-run=server*) test "${TAK_TEST_FAILURE:-}" != validation ;;
  *delete*) test "${TAK_TEST_FAILURE:-}" != deletion ;;
esac
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["", "validation", "deletion"])
async def test_cutover_validates_and_releases_old_owner_before_applying(tmp_path: Path, failure: str) -> None:
    executable = tmp_path / "kubectl"
    executable.write_text(FAKE_KUBECTL)
    executable.chmod(0o700)
    log = tmp_path / "commands.log"
    process = await asyncio.create_subprocess_exec(
        "bash",
        str(ROOT / "deploy/scripts/deploy-tak.sh"),
        "test-context",
        "test-namespace",
        "test-overlay",
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "TAK_TEST_LOG": str(log),
            "TAK_TEST_FAILURE": failure,
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    commands = log.read_text().splitlines()
    assert all(command.startswith("--context test-context -n test-namespace ") for command in commands)
    assert "apply --dry-run=server -k test-overlay" in commands[0]
    if failure == "validation":
        assert process.returncode != 0
        assert len(commands) == 1  # No interruption when API validation fails.
        return
    assert "delete deployment/takserver --ignore-not-found --cascade=foreground --wait=true" in commands[1]
    if failure == "deletion":
        assert process.returncode != 0
        assert len(commands) == 2  # Never start two shared-config owners.
        return
    assert process.returncode == 0, output.decode()
    assert commands[2].endswith("apply -k test-overlay")
    assert len(commands) == 6
    assert all("rollout status" in command for command in commands[3:])
    assert {command.split("deployment/")[1].split()[0] for command in commands[3:]} == {
        "tak-config",
        "tak-messaging",
        "tak-api",
    }


def test_split_roles_have_bootstrap_discovery_and_one_shared_volume_owner() -> None:
    resources = list(yaml.safe_load_all((ROOT / "deploy/base/takserver.yaml").read_text()))
    deployments = [resource for resource in resources if resource["kind"] == "Deployment"]
    assert len(deployments) == 3
    services = {
        resource["metadata"]["name"]: resource["spec"] for resource in resources if resource["kind"] == "Service"
    }
    discovery = services["tak-ignite"]
    assert discovery["clusterIP"] == "None"
    assert discovery["publishNotReadyAddresses"] is True
    selected = {}
    owners = []
    for deployment in deployments:
        template = deployment["spec"]["template"]
        pod = template["spec"]
        assert len(pod["containers"]) == 1
        container = pod["containers"][0]
        role = container["args"][-1]
        assert container["readinessProbe"]["exec"]["command"][-1] == role
        assert container["startupProbe"]["exec"]["command"][-1] == role
        bindings = {env["name"]: env for env in container["env"]}
        assert bindings["TAK_IGNITE_BIND_ADDRESS"]["valueFrom"]["fieldRef"]["fieldPath"] == "status.podIP"
        mounts = {mount["mountPath"]: mount["name"] for mount in container["volumeMounts"]}
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        assert "emptyDir" in volumes[mounts["/opt/tak/runtime"]]
        assert volumes[mounts["/opt/tak/data"]]["persistentVolumeClaim"]["claimName"] == "tak-data"
        assert "/etc/default/takserver" in mounts  # Preserve CNPG TLS 1.3 support.
        if pod.get("initContainers"):
            owners.append(role)
            assert deployment["spec"]["strategy"]["type"] == "Recreate"
        else:
            affinity = pod["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
            assert affinity[0]["topologyKey"] == "kubernetes.io/hostname"
            assert affinity[0]["labelSelector"]["matchLabels"]["app.kubernetes.io/component"] == "config"
        for name, service in services.items():
            if service["selector"].items() <= template["metadata"]["labels"].items():
                selected.setdefault(name, []).append(role)
    assert owners == ["config"]
    assert selected == {"takserver": ["api"], "tak-messaging": ["messaging"], "tak-ignite": ["messaging"]}
