#!/usr/bin/env bash
# The sibling's `task up` owns a foreground Tilt session; wait for readiness, not exit.
set -euo pipefail

sibling_dir=$(cd "${1:?Pass the sibling repository directory}" && pwd -P)
export TILT_PORT="${2:-10350}"
startup_timeout="${PLATFORM_START_TIMEOUT:-600}"
if [[ ! "$startup_timeout" =~ ^[1-9][0-9]*$ ]]; then
    echo 'PLATFORM_START_TIMEOUT must be a positive number of seconds.' >&2
    exit 1
fi
context=kind-rmk8soperator
log_file="$(pwd)/.task/platform-up.log"

for program in task tilt kubectl; do
    if ! command -v "$program" >/dev/null 2>&1; then
        echo "Missing $program; activate the sibling repository's mise tools first." >&2
        exit 1
    fi
done

session_exists() {
    local session
    if session=$(tilt get session Tiltfile --host 127.0.0.1 --port "$TILT_PORT" \
        -o jsonpath='{.spec.tiltfilePath}' 2>&1); then
        if [[ "$session" != "$sibling_dir/Tiltfile" ]]; then
            echo "Tilt on port $TILT_PORT belongs to $session; use a different TILT_PORT." >&2
            exit 1
        fi
        return 0
    fi
    case "$session" in
        *'No tilt apiserver found'*|*'connection refused'*|*'(NotFound)'*) return 1 ;;
        # Tilt's kubectl client can report a stale API endpoint in this form.
        *'The connection to the server '*' was refused'*) return 1 ;;
        *) printf '%s\n' "$session" >&2; exit 1 ;;
    esac
}

if session_exists; then
    echo "Reusing the sibling Tilt session on port $TILT_PORT."
    # Repair/recheck the registry connection as well as the cluster itself.
    task --dir "$sibling_dir" cluster:up
else
    mkdir -p "$(dirname "$log_file")"
    echo "Starting the sibling's task up in the background; logs: $log_file"
    nohup task --dir "$sibling_dir" up > "$log_file" 2>&1 < /dev/null &
    launcher_pid=$!
    deadline=$((SECONDS + startup_timeout))
    until session_exists; do
        if ! kill -0 "$launcher_pid" 2>/dev/null; then
            echo "The sibling's task up exited before Tilt started. Last log lines:" >&2
            tail -n 30 "$log_file" >&2
            exit 1
        fi
        if (( SECONDS >= deadline )); then
            echo "Timed out waiting for Tilt; inspect $log_file." >&2
            exit 1
        fi
        sleep 1
    done
fi

# The platform resource is created only after the webhook CA is exported and
# the Tiltfile reloads. Demo resources deliberately remain a manual Tilt action.
tilt wait --host 127.0.0.1 --port "$TILT_PORT" \
    --for=create --for=condition=UpToDate --for=condition=Ready --timeout="${startup_timeout}s" \
    uiresource/opendefence-platform
kubectl --context "$context" wait --for=condition=Established --timeout=60s \
    crd/users.platform.opendefence.fi crd/groups.platform.opendefence.fi crd/roles.platform.opendefence.fi
kubectl --context "$context" -n opendefence-system rollout status \
    deployment/opendefence-platform --timeout="${startup_timeout}s"
