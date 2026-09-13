"""Check fresh credentials, repeat startup, and legacy database password reuse."""

import asyncio
import os
import secrets
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/scripts/ensure-secrets.sh"
FAKE_KUBECTL = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$SECRET_TEST_ROOT/commands.log"
while [[ "$1" == --context || "$1" == -n ]]; do shift 2; done
case "$1" in
  apply) echo 'namespace unchanged' ;;
  get)
    secret="$SECRET_TEST_ROOT/$3"
    [[ -f "$secret" ]] || exit 1
    if [[ "${4:-}" == -o ]]; then
      if [[ -f "$SECRET_TEST_ROOT/legacy-password" ]]; then
        cat "$SECRET_TEST_ROOT/legacy-password"
      fi
    fi
    ;;
  create)
    secret="$SECRET_TEST_ROOT/$4"
    shift 4
    for argument in "$@"; do
      case "$argument" in
        --from-env-file=*) cp "${argument#*=}" "$secret" ;;
        --from-file=password=*) cp "${argument#*=password=}" "$secret" ;;
      esac
    done
    echo 'secret created'
    ;;
  *) exit 1 ;;
esac
"""


async def run_helper(directory: Path) -> str:
    """Execute the real helper with only the Kubernetes API replaced."""
    executables = directory / "bin"
    executables.mkdir(exist_ok=True)
    kubectl = executables / "kubectl"
    kubectl.write_text(FAKE_KUBECTL)
    kubectl.chmod(0o700)
    process = await asyncio.create_subprocess_exec(
        "bash",
        str(SCRIPT),
        env={
            **os.environ,
            "PATH": f"{executables}{os.pathsep}{os.environ['PATH']}",
            "SECRET_TEST_ROOT": str(directory),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode == 0, output.decode()
    return output.decode()


@pytest.mark.asyncio
async def test_fresh_credentials_are_private_and_reruns_preserve_them(tmp_path: Path) -> None:
    output = await run_helper(tmp_path)
    certificates = (tmp_path / "tak-secrets").read_text()
    password = (tmp_path / "tak-database-app").read_text()
    assert len(password) == 48
    assert int(password, 16) > 0
    assert "POSTGRES_PASSWORD" not in certificates
    assert {line.split("=", 1)[0] for line in certificates.splitlines()} == {
        "TAKSERVER_CERT_PASS",
        "CA_PASS",
        "ADMIN_CERT_PASS",
    }
    assert (
        "--type=kubernetes.io/basic-auth --from-literal=username=martiuser" in (tmp_path / "commands.log").read_text()
    )
    assert password not in output
    assert (tmp_path / "tak-database-app").stat().st_mode & 0o077 == 0
    assert (tmp_path / "tak-secrets").stat().st_mode & 0o077 == 0
    await run_helper(tmp_path)
    assert (tmp_path / "tak-secrets").read_text() == certificates
    assert (tmp_path / "tak-database-app").read_text() == password
    assert (tmp_path / "commands.log").read_text().count("create secret") == 2


@pytest.mark.asyncio
async def test_legacy_database_password_is_copied_without_rotating_certificates(tmp_path: Path) -> None:
    (tmp_path / "tak-secrets").write_text("existing certificate credentials")
    password = f"{secrets.token_urlsafe(16)} / with spaces"
    (tmp_path / "legacy-password").write_text(password)
    output = await run_helper(tmp_path)
    assert (tmp_path / "tak-database-app").read_text() == password
    assert (tmp_path / "tak-secrets").read_text() == "existing certificate credentials"
    assert password not in output
    assert password not in (tmp_path / "commands.log").read_text()


@pytest.mark.asyncio
async def test_existing_cnpg_credentials_take_precedence_over_legacy_password(tmp_path: Path) -> None:
    (tmp_path / "tak-secrets").write_text("existing certificate credentials")
    (tmp_path / "tak-database-app").write_text("current database password")
    (tmp_path / "legacy-password").write_text("obsolete password")
    await run_helper(tmp_path)
    assert (tmp_path / "tak-database-app").read_text() == "current database password"
    assert "create secret" not in (tmp_path / "commands.log").read_text()


@pytest.mark.asyncio
async def test_missing_certificates_do_not_replace_existing_cnpg_credentials(tmp_path: Path) -> None:
    (tmp_path / "tak-database-app").write_text("existing database password")
    await run_helper(tmp_path)
    assert (tmp_path / "tak-secrets").is_file()
    assert (tmp_path / "tak-database-app").read_text() == "existing database password"
