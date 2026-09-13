"""Exercise tool resolution with unusable shims, without activating real mise."""

import asyncio
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/scripts/with-platform-tools.sh"


def executable(directory: Path, name: str, content: str) -> None:
    """Write a fake command that can be resolved from PATH."""
    command = directory / name
    command.write_text(f"#!/bin/bash\nset -eu\n{content}")
    command.chmod(0o700)


async def run_wrapper(directory: Path, *, mise: bool, failure: bool = False) -> tuple[int, str]:
    """Keep broken shims first until the wrapper resolves the sibling's tools."""
    shims = directory / "shims"
    installed = directory / "installed tools"
    sibling = directory / "sibling checkout"
    for folder in (shims, installed, sibling):
        folder.mkdir()
    expected = 'printf "%s\\n" "$PWD" "$*"\n'
    executable(installed, "tilt", expected)
    if mise:
        executable(shims, "tilt", 'echo "No version is set for shim: tilt" >&2\nexit 1\n')
        executable(
            shims,
            "mise",
            r"""[[ "$*" == "exec -C $TOOL_TEST_SIBLING -- printenv PATH" ]]
if [[ "$TOOL_TEST_FAILURE" == 1 ]]; then
    echo 'Tool resolution failed' >&2
    exit 42
fi
printf '%s:%s\n' "$TOOL_TEST_INSTALLED" "$PATH"
""",
        )
    else:
        executable(shims, "tilt", expected)
    process = await asyncio.create_subprocess_exec(
        "/bin/bash",
        str(SCRIPT),
        str(sibling),
        "tilt",
        "get",
        "an argument with spaces",
        cwd=directory,
        env={
            **os.environ,
            # Exclude host mise so the fallback test is independent of the workstation.
            "PATH": f"{shims}:/usr/bin:/bin",
            "TOOL_TEST_SIBLING": str(sibling),
            "TOOL_TEST_INSTALLED": str(installed),
            "TOOL_TEST_FAILURE": str(int(failure)),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode is not None
    return process.returncode, output.decode()


@pytest.mark.asyncio
async def test_resolves_sibling_tools_ahead_of_broken_shims_without_changing_cwd(tmp_path: Path) -> None:
    code, output = await run_wrapper(tmp_path, mise=True)
    assert code == 0, output
    assert output.splitlines() == [str(tmp_path), "get an argument with spaces"]


@pytest.mark.asyncio
async def test_uses_existing_tools_when_mise_is_not_installed(tmp_path: Path) -> None:
    code, output = await run_wrapper(tmp_path, mise=False)
    assert code == 0, output
    assert output.splitlines() == [str(tmp_path), "get an argument with spaces"]


@pytest.mark.asyncio
async def test_failed_tool_resolution_stops_before_running_a_shim(tmp_path: Path) -> None:
    code, output = await run_wrapper(tmp_path, mise=True, failure=True)
    assert code == 42
    assert output.strip() == "Tool resolution failed"
