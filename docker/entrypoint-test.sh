#!/bin/bash -l
set -e
# shellcheck disable=SC1091
source /.venv/bin/activate
if [ "$#" -eq 0 ]; then
  # Kill cache, pytest complains about it if running local and docker tests in mapped volume
  find tests  -type d -name '__pycache__' -print0 | xargs -0 rm -rf {}
  # Make sure the service itself is installed
  uv sync --locked
  # Make sure prek checks were not missed because reasons
  uv run --locked prek run --all-files
  # Then run the tests
  uv run --locked pytest --junitxml=pytest.xml tests/
  # If prek does not run these, enable them
  # pyrefly check
  # ruff check src tests
  # ruff format --check src tests
  # bandit --skip=B101 -r src
else
  exec "$@"
fi
