"""Exercise sibling startup, session ownership, and readiness with fake CLIs."""

import asyncio
import os
import signal
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/scripts/ensure-platform.sh"
FAKE_CLI = r"""#!/usr/bin/env bash
set -eu
program=${0##*/}
printf '%s\n' "$program $*" >> "$PLATFORM_TEST_LOG"
case "$program" in
  tilt)
    if [ "$1" = get ]; then
      if [ "$PLATFORM_TEST_MODE" = foreign ]; then
        echo /another/checkout/Tiltfile
      elif [ -f "$PLATFORM_TEST_SESSION" ]; then
        printf '%s/Tiltfile' "$PLATFORM_TEST_REPO"
      else
        echo 'No tilt apiserver found' >&2
        exit 1
      fi
    elif [ "$PLATFORM_TEST_MODE" = unready ]; then
      echo 'Platform is not Ready' >&2
      exit 1
    fi
    ;;
  task)
    if [ "${!#}" = up ]; then
      if [ "$PLATFORM_TEST_MODE" = failed ]; then
        echo 'Sibling startup failed' >&2
        exit 1
      fi
      echo $$ > "$PLATFORM_TEST_PID"
      touch "$PLATFORM_TEST_SESSION"
      exec sleep 30
    fi
    ;;
  kubectl) ;;
  *) exit 1 ;;
esac
"""


@dataclass
class PlatformHarness:
    """Local commands with an observable, independently running fake Tilt owner."""

    directory: Path
    sibling: Path
    environment: dict[str, str]

    async def run(self, mode: str, *, existing: bool) -> tuple[int, str]:
        if existing:
            Path(self.environment["PLATFORM_TEST_SESSION"]).touch()
        process = await asyncio.create_subprocess_exec(
            "bash",
            str(SCRIPT),
            str(self.sibling),
            "10400",
            cwd=self.directory,
            env={**self.environment, "PLATFORM_TEST_MODE": mode},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode is not None
        return process.returncode, output.decode()

    def commands(self) -> str:
        return Path(self.environment["PLATFORM_TEST_LOG"]).read_text()


@pytest.fixture
def platform(tmp_path: Path) -> Iterator[PlatformHarness]:
    sibling = tmp_path / "sibling checkout"
    sibling.mkdir()
    executables = tmp_path / "bin"
    executables.mkdir()
    for name in ("task", "tilt", "kubectl"):
        program = executables / name
        program.write_text(FAKE_CLI)
        program.chmod(0o700)
    environment = {
        **os.environ,
        "PATH": f"{executables}{os.pathsep}{os.environ['PATH']}",
        "PLATFORM_TEST_LOG": str(tmp_path / "commands.log"),
        "PLATFORM_TEST_SESSION": str(tmp_path / "session"),
        "PLATFORM_TEST_PID": str(tmp_path / "pid"),
        "PLATFORM_TEST_REPO": str(sibling.resolve()),
        "PLATFORM_START_TIMEOUT": "5",
    }
    try:
        yield PlatformHarness(tmp_path, sibling, environment)
    finally:
        pid_file = Path(environment["PLATFORM_TEST_PID"])
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_reuses_matching_session_and_checks_real_cluster_readiness(platform: PlatformHarness) -> None:
    code, output = await platform.run("ready", existing=True)
    assert code == 0, output
    assert "Reusing" in output
    commands = platform.commands()
    assert f"task --dir {platform.sibling.resolve()} cluster:up" in commands
    assert f"task --dir {platform.sibling.resolve()} up\n" not in commands
    assert "--for=create --for=condition=UpToDate --for=condition=Ready" in commands
    assert "crd/users.platform.opendefence.fi" in commands
    assert "rollout status deployment/opendefence-platform" in commands


@pytest.mark.asyncio
async def test_starts_foreground_sibling_task_in_background_and_returns_when_ready(platform: PlatformHarness) -> None:
    code, output = await platform.run("ready", existing=False)
    assert code == 0, output
    assert f"task --dir {platform.sibling.resolve()} up\n" in platform.commands()
    assert "--port 10400" in platform.commands()
    assert (platform.directory / ".task/platform-up.log").is_file()
    # Startup completes while the sibling's foreground process remains running.
    os.kill(int(Path(platform.environment["PLATFORM_TEST_PID"]).read_text()), 0)


@pytest.mark.asyncio
async def test_foreign_tilt_session_is_rejected_without_starting_or_changing_cluster(platform: PlatformHarness) -> None:
    code, output = await platform.run("foreign", existing=True)
    assert code != 0
    assert "another/checkout" in output
    assert "task --dir" not in platform.commands()
    assert "kubectl" not in platform.commands()


@pytest.mark.asyncio
async def test_sibling_failure_stops_before_readiness_or_deployment(platform: PlatformHarness) -> None:
    code, output = await platform.run("failed", existing=False)
    assert code != 0
    assert "Sibling startup failed" in output
    assert "tilt wait" not in platform.commands()
    assert "kubectl" not in platform.commands()


@pytest.mark.asyncio
async def test_unready_platform_does_not_report_success(platform: PlatformHarness) -> None:
    code, output = await platform.run("unready", existing=True)
    assert code != 0
    assert "Platform is not Ready" in output
    assert "kubectl" not in platform.commands()
