#!/usr/bin/env bash
# Resolve mise shims using the sibling's configuration without changing our cwd.
set -euo pipefail
sibling_dir="${1:?Pass the sibling repository directory}"
shift
if command -v mise >/dev/null 2>&1; then
    tool_path=$(mise exec -C "$sibling_dir" -- printenv PATH)
    export PATH="$tool_path"
fi
exec "$@"
