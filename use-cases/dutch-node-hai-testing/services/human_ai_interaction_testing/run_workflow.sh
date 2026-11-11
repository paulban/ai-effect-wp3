#!/usr/bin/env bash
#
# Run a human-AI interaction session through the orchestrator (spec FR-30).
#
#   submit ─▶ orchestrator ─▶ worker ─▶ hai-session-runner:8080/control/execute
#                                              │ job id, immediately
#   this script ◀── participant links ─────────┤ /control/intermediate/{job_id}
#                                              │ (placeholder until the orchestrator
#                                              │  relays intermediate results)
#                              poll ◀──────────┤ /control/status   (until the deadline)
#   this script ◀── DataReference ◀────────────┘ /control/output
#        └──── GET the artifact URL ────────────▶ /control/data/{job_id}
#
# The intermediate result and the artifact are read from the loopback port the
# service publishes (HAI_HOST_PORT, default 8005). The orchestrator does not
# expose the service's job id, but it is deterministic: <workflow id>.<task id>
# (common.batch_jobs.build_job_id).
#
# Usage:
#   ./run_workflow.sh                  # request built from the variables below
#   ./run_workflow.sh request.json     # request taken from a JSON file (spec 5.3)
#
# Environment:
#   ORCHESTRATOR_URL          Default http://localhost:18000
#   ORCHESTRATOR_API_KEY      Bearer token on the orchestrator API, if any
#   SERVICE_API_KEY           Bearer token on the service's /control/*; must match the container's
#   HAI_HOST_PORT             Loopback port of hai-session-runner. Default 8005
#   ALGORITHM                 File in algorithms/. Default algorithm_template.py
#   ENV_NAME                  Environment directory in scenarios/. Default env_ICAPS_input_data_test
#   SCENARIO_NAME             Chronic folder in <ENV_NAME>/chronics. Default 2035-01-01_0
#   SCENARIO_FIRST_STEP       Default 0
#   SESSION_DURATION_SECONDS  Default 900 (15 minutes)
#   OUTPUT_DIR                Where the result is written. Default ./results

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPORT_DIR="$SCRIPT_DIR/../../export"

ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://localhost:18000}"
ORCHESTRATOR_API_KEY="${ORCHESTRATOR_API_KEY:-}"
SERVICE_API_KEY="${SERVICE_API_KEY:-}"
SERVICE_LOCAL_URL="http://localhost:${HAI_HOST_PORT:-8005}"
ALGORITHM="${ALGORITHM:-algorithm_template.py}"
ENV_NAME="${ENV_NAME:-env_ICAPS_input_data_test}"
SCENARIO_NAME="${SCENARIO_NAME:-2035-01-01_0}"
SCENARIO_FIRST_STEP="${SCENARIO_FIRST_STEP:-0}"
SESSION_DURATION_SECONDS="${SESSION_DURATION_SECONDS:-900}"
OUTPUT_DIR="${OUTPUT_DIR:-$SCRIPT_DIR/results}"
REQUEST_FILE="${1:-}"

POLL_INTERVAL=15
ORCHESTRATOR_PROJECT="${ORCHESTRATOR_PROJECT:-orchestrator}"

# --- Tooling ---------------------------------------------------------------------

PYTHON_BIN=""
for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import sys" >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"; break
    fi
done
[ -n "$PYTHON_BIN" ] || { echo "No working Python 3 found." >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "curl not found." >&2; exit 1; }

# Read one value out of a JSON document by dotted path; empty when absent.
json_field() {
    printf '%s' "$1" | "$PYTHON_BIN" -c '
import json, sys
try:
    value = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for key in sys.argv[1].split("."):
    if isinstance(value, list):
        index = int(key)
        value = value[index] if -len(value) <= index < len(value) else None
    elif isinstance(value, dict):
        value = value.get(key)
    else:
        value = None
print("" if value is None else value)
' "$2" 2>/dev/null
}

json_pretty() {
    printf '%s' "$1" | "$PYTHON_BIN" -c 'import json, sys
raw = sys.stdin.read()
try:
    print(json.dumps(json.loads(raw), indent=2))
except ValueError:
    print(raw)'
}

# GET with a bearer token when one is given; prints the body.
api_get() {
    local url="$1" token="${2:-}"
    if [ -n "$token" ]; then curl -s -H "Authorization: Bearer $token" "$url"; else curl -s "$url"; fi
}

# GET returning only the HTTP status code.
api_status() {
    local url="$1" token="${2:-}"
    if [ -n "$token" ]; then
        curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" "$url" || echo 000
    else
        curl -s -o /dev/null -w '%{http_code}' "$url" || echo 000
    fi
}

# --- Step 0: pre-flight ----------------------------------------------------------

echo "=========================================="
echo "Dutch node — human-AI interaction session"
echo "=========================================="
echo

for required_file in "$EXPORT_DIR/blueprint.json" "$EXPORT_DIR/dockerinfo.json"; do
    [ -f "$required_file" ] || { echo "Missing $required_file (scripts/onboarding-export-generator.py)." >&2; exit 1; }
done
DOCKERINFO=$(cat "$EXPORT_DIR/dockerinfo.json")
SERVICE_HOST=$(json_field "$DOCKERINFO" "docker_info_list.0.ip_address")
SERVICE_PORT=$(json_field "$DOCKERINFO" "docker_info_list.0.port")

echo "Step 0: pre-flight"
printf "  orchestrator (%s) ... " "$ORCHESTRATOR_URL"
if [ "$(json_field "$(api_get "$ORCHESTRATOR_URL/health" "$ORCHESTRATOR_API_KEY" || true)" "status")" = "ok" ]; then
    echo "ok"
else
    echo "UNREACHABLE"; echo "  Start it with:  cd orchestrator && docker compose up -d" >&2; exit 1
fi

printf "  orchestrator workers ... "
WORKERS=$(docker ps --format '{{.Names}}' \
    --filter "label=com.docker.compose.project=$ORCHESTRATOR_PROJECT" \
    --filter "label=com.docker.compose.service=worker" 2>/dev/null || true)
FIRST_WORKER=$(printf '%s' "$WORKERS" | head -n 1)
if [ -n "$FIRST_WORKER" ]; then
    echo "$(printf '%s' "$WORKERS" | grep -c .) running"
else
    echo "NONE RUNNING"; echo "  Start them with:  cd orchestrator && docker compose up -d worker" >&2; exit 1
fi

# Probed from inside a worker: same network, same name the orchestrator dials.
printf "  %s from a worker ... " "$SERVICE_HOST:$SERVICE_PORT"
if docker exec "$FIRST_WORKER" python -c "
import sys, urllib.request
urllib.request.urlopen('http://$SERVICE_HOST:$SERVICE_PORT/health', timeout=5)
" >/dev/null 2>&1; then
    echo "ok"
else
    echo "UNREACHABLE"
    echo "  Start the service:  cd use-cases/dutch-node-hai-testing && docker compose up -d --build" >&2
    exit 1
fi

printf "  service api key ... "
case "$(api_status "$SERVICE_LOCAL_URL/control/status/preflight" "$SERVICE_API_KEY")" in
    404) echo "accepted" ;;
    401) echo "REJECTED (must equal the container's SERVICE_API_KEY)" >&2; exit 1 ;;
    *)   echo "inconclusive (is $SERVICE_LOCAL_URL published?)" ;;
esac
echo

# --- Step 1: the request ---------------------------------------------------------

echo "Step 1: session request"
if [ -n "$REQUEST_FILE" ]; then
    [ -f "$REQUEST_FILE" ] || { echo "No such file: $REQUEST_FILE" >&2; exit 1; }
    REQUEST_JSON=$(cat "$REQUEST_FILE")
else
    REQUEST_JSON=$("$PYTHON_BIN" - "$ALGORITHM" "$ENV_NAME" "$SCENARIO_NAME" "$SCENARIO_FIRST_STEP" "$SESSION_DURATION_SECONDS" <<'PY'
import json, sys
algorithm, env_name, scenario_name, first_step, duration = sys.argv[1:6]
print(json.dumps({
    "algorithm": algorithm,
    "scenario": {"env_name": env_name, "scenario_name": scenario_name, "scenario_first_step": int(first_step)},
    "session_duration_seconds": int(duration),
}))
PY
)
fi
json_pretty "$REQUEST_JSON" | sed 's/^/  /'
DURATION=$(json_field "$REQUEST_JSON" "session_duration_seconds")
echo

# --- Step 2: submit --------------------------------------------------------------

echo "Step 2: submitting to $ORCHESTRATOR_URL/workflows"
PAYLOAD=$("$PYTHON_BIN" - "$EXPORT_DIR" "$REQUEST_JSON" "$SERVICE_API_KEY" <<'PY'
import base64, json, pathlib, sys
export_directory, request_json, service_api_key = sys.argv[1:4]
export_path = pathlib.Path(export_directory)
payload = {
    "blueprint": json.loads((export_path / "blueprint.json").read_text(encoding="utf-8")),
    "dockerinfo": json.loads((export_path / "dockerinfo.json").read_text(encoding="utf-8")),
    "inputs": [{
        "protocol": "inline",
        "uri": base64.b64encode(request_json.encode("utf-8")).decode("ascii"),
        "format": "json",
    }],
}
if service_api_key:
    payload["services_api_key"] = service_api_key
print(json.dumps(payload))
PY
)
SUBMIT_ARGS=(-s -X POST "$ORCHESTRATOR_URL/workflows" -H "Content-Type: application/json")
[ -n "$ORCHESTRATOR_API_KEY" ] && SUBMIT_ARGS+=(-H "Authorization: Bearer $ORCHESTRATOR_API_KEY")
RESPONSE=$(printf '%s' "$PAYLOAD" | curl "${SUBMIT_ARGS[@]}" -d @-)
WORKFLOW_ID=$(json_field "$RESPONSE" "workflow_id")
[ -n "$WORKFLOW_ID" ] || { echo "  Submission rejected:"; json_pretty "$RESPONSE"; exit 1; }
echo "  workflow id: $WORKFLOW_ID"
echo

# --- Step 3: participant links (intermediate result) ------------------------------

echo "Step 3: waiting for the session to go live"
JOB_ID=""
for _ in $(seq 1 40); do
    TASKS=$(api_get "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID/tasks" "$ORCHESTRATOR_API_KEY" || true)
    TASK_ID=$(json_field "$TASKS" "tasks.0.task_id")
    TASK_STATUS=$(json_field "$TASKS" "tasks.0.status")
    if [ "$TASK_STATUS" = "failed" ] || [ "$TASK_STATUS" = "FAILED" ]; then
        echo "  The session failed before going live:"; json_pretty "$TASKS"; exit 1
    fi
    if [ -n "$TASK_ID" ]; then
        JOB_ID="$WORKFLOW_ID.$TASK_ID"
        if [ "$(api_status "$SERVICE_LOCAL_URL/control/intermediate/$JOB_ID" "$SERVICE_API_KEY")" = "200" ]; then
            break
        fi
    fi
    sleep 3
done
INTERMEDIATE=$(api_get "$SERVICE_LOCAL_URL/control/intermediate/$JOB_ID" "$SERVICE_API_KEY" || true)
if [ -z "$(json_field "$INTERMEDIATE" "frontend_url")" ]; then
    echo "  No intermediate result after 2 minutes (job $JOB_ID). Service logs:"
    echo "    docker logs hai-session-runner --tail 50"
    exit 1
fi
echo "  Session $JOB_ID is live until $(json_field "$INTERMEDIATE" "deadline")."
echo "  Send this to the participant:"
echo
"$PYTHON_BIN" -c '
import json, sys
intermediate = json.loads(sys.argv[1])
for number, step in enumerate(intermediate["instructions"], 1):
    print(f"    {number}. {step}")
' "$INTERMEDIATE"
echo

# --- Step 4: wait for the deadline -----------------------------------------------

echo "Step 4: waiting for the deadline (${DURATION}s, polled every ${POLL_INTERVAL}s)"
MAX_POLLS=$(( (DURATION + 600) / POLL_INTERVAL ))
STATUS=""
for _ in $(seq 1 "$MAX_POLLS"); do
    STATUS=$(json_field "$(api_get "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID" "$ORCHESTRATOR_API_KEY" || true)" "status")
    case "$STATUS" in
        completed|COMPLETED) echo; echo "  completed"; break ;;
        failed|FAILED|error|ERROR)
            echo; echo "  failed:"; json_pretty "$(api_get "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID/tasks" "$ORCHESTRATOR_API_KEY")"; exit 1 ;;
        *) printf "."; sleep "$POLL_INTERVAL" ;;
    esac
done
case "$STATUS" in completed|COMPLETED) ;; *) echo; echo "  Still $STATUS after the deadline plus 10 minutes." >&2; exit 1 ;; esac
echo

# --- Step 5: fetch the result ------------------------------------------------------

echo "Step 5: fetching the result"
TASKS=$(api_get "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID/tasks" "$ORCHESTRATOR_API_KEY")
RESULT_URI=$(json_field "$TASKS" "tasks.0.output_refs.0.uri")
[ -n "$RESULT_URI" ] || { echo "  No output reference:"; json_pretty "$TASKS"; exit 1; }
mkdir -p "$OUTPUT_DIR"
RESULT_FILE="$OUTPUT_DIR/$WORKFLOW_ID.json"
FETCH_ARGS=(-s -w '%{http_code}' -o "$RESULT_FILE")
[ -n "$SERVICE_API_KEY" ] && FETCH_ARGS+=(-H "Authorization: Bearer $SERVICE_API_KEY")
set +e
HTTP_CODE=$(curl "${FETCH_ARGS[@]}" "$RESULT_URI")
set -e
if [ "$HTTP_CODE" != "200" ]; then
    echo "  HTTP $HTTP_CODE fetching $RESULT_URI"; rm -f "$RESULT_FILE"; exit 1
fi
echo "  written to $RESULT_FILE"
echo

"$PYTHON_BIN" - "$RESULT_FILE" <<'PY'
import collections, json, pathlib, sys
result = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
metadata, session = result["metadata"], result["session"]
traces = result.get("traces") or []
steps = collections.Counter(str(trace.get("step")) for trace in traces if isinstance(trace, dict))
print("  Session result:")
print(f"    algorithm:  {metadata['algorithm']}")
print(f"    window:     {session['started_at']} -> {session['deadline']}")
print(f"    traces:     {len(traces)} " + (f"({', '.join(f'{k}: {v}' for k, v in sorted(steps.items()))})" if steps else ""))
print(f"    query:      {metadata['trace_query'].get('url')}")
for warning in metadata.get("warnings", []):
    print(f"    WARNING:    {warning}")
for name, reason in result.get("not_collected", {}).items():
    print(f"    not collected ({name}): {reason}")
PY

echo
echo "=========================================="
echo "Done. Workflow $WORKFLOW_ID"
echo "=========================================="
