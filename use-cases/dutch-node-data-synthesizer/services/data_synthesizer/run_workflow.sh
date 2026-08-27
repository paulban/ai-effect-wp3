#!/usr/bin/env bash
#
# Run a grid synthesis through the orchestrator, the way the AI vendor does.
#
# The service publishes no host port (FR-19, FR-27). It is reachable only by the
# orchestrator's workers, on the shared Docker network, at the address
# export/dockerinfo.json names. So this script never calls the service to submit
# work: it hands the orchestrator a blueprint and lets a worker make that call.
#
# The one leg it does walk itself is the last one. A finished task carries a
# DataReference with an HTTP URL, and fetching it is what turns a completed
# workflow into a result someone can use (FR-05, FR-25). A run that stops at the
# task listing has not demonstrated the thing the redesign was for.
#
#   submit ─▶ orchestrator ─▶ worker ─▶ synthetic-data:8080/control/execute
#                                              │ task id, immediately
#                              poll ◀──────────┘ /control/status
#                                              │
#   this script ◀── DataReference ◀────────────┘ /control/output
#        └──── GET the artifact URL ────────────▶ /control/data/{task_id}
#
# Environment:
#   ORCHESTRATOR_URL      Default http://localhost:18000
#   ORCHESTRATOR_API_KEY  Bearer token on the orchestrator API, if it has one
#   SERVICE_API_KEY       Bearer token on the service's /control/*. Must match
#                         what the service container was started with, or the
#                         worker's call is rejected with 401
#   OUTPUT_DIR            Where the fetched grid is written. Default ./results

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPORT_DIR="$SCRIPT_DIR/../../export"

ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://localhost:18000}"
ORCHESTRATOR_API_KEY="${ORCHESTRATOR_API_KEY:-}"
SERVICE_API_KEY="${SERVICE_API_KEY:-}"
OUTPUT_DIR="${OUTPUT_DIR:-$SCRIPT_DIR/results}"

# Synthesis of a default three-level grid takes seconds, not minutes, but the
# worker polls the service on a five-second cycle of its own.
POLL_INTERVAL="${POLL_INTERVAL:-3}"
MAX_POLLS="${MAX_POLLS:-100}"

# The compose project the orchestrator runs under, used only to count its
# workers. Override if you renamed the directory.
ORCHESTRATOR_PROJECT="${ORCHESTRATOR_PROJECT:-orchestrator}"

# ---------------------------------------------------------------------------
# Tooling
# ---------------------------------------------------------------------------

# Windows ships a `python3` stub that exits non-zero and advertises the Store,
# so presence on PATH is not enough — each candidate has to actually run.
PYTHON_BIN=""
for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import sys" >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "No working Python interpreter found. This script assembles and reads" >&2
    echo "JSON with it; install Python 3 or run it from the service's venv." >&2
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "curl not found on PATH." >&2
    exit 1
fi

# Read one value out of a JSON document by dotted path. Missing keys and null
# values both yield an empty string, so callers can test with -z.
json_field() {
    printf '%s' "$1" | "$PYTHON_BIN" -c '
import json, sys

try:
    value = json.load(sys.stdin)
except ValueError:
    sys.exit(0)

for key in sys.argv[1].split("."):
    if value is None:
        break
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
    printf '%s' "$1" | "$PYTHON_BIN" -c '
import json, sys

raw = sys.stdin.read()
try:
    print(json.dumps(json.loads(raw), indent=2))
except ValueError:
    print(raw)
'
}

# Report the status code /control/status gives for an unknown task id, called
# from inside a worker with the token we hold.
#
# This is how the script learns whether the service's control plane is open or
# guarded, and whether our key is the right one. /health cannot answer it: it is
# deliberately unauthenticated, so it says "ok" to a caller whose every
# /control/* call will be rejected. /control/status is guarded, and an unknown
# task id separates the two cases cleanly — 404 means the call was authorised
# and the task simply does not exist, 401 means it was not.
probe_control_auth() {
    docker exec "$FIRST_WORKER" python -c '
import sys, urllib.error, urllib.request

url, token = sys.argv[1], sys.argv[2]
request = urllib.request.Request(url)
if token:
    request.add_header("Authorization", "Bearer " + token)

try:
    with urllib.request.urlopen(request, timeout=5) as response:
        print(response.status)
except urllib.error.HTTPError as http_error:
    print(http_error.code)
except Exception:
    print(0)
' "http://$SERVICE_HOST:$SERVICE_PORT/control/status/preflight" "$1" 2>/dev/null
}

# curl with whichever bearer token the target expects. Prints the body; returns
# the exit status of curl so a caller can distinguish "no answer" from "an
# answer we did not like".
api_get() {
    local url="$1" token="${2:-}"
    if [ -n "$token" ]; then
        curl -s -H "Authorization: Bearer $token" "$url"
    else
        curl -s "$url"
    fi
}

# ---------------------------------------------------------------------------
# Step 0 — pre-flight
#
# Each check below corresponds to a way this has actually failed. Probing a
# host port the service no longer publishes was the first; a stack whose
# workers had been stopped, so every workflow sat at "running" until the poll
# budget ran out, was the second.
# ---------------------------------------------------------------------------

echo "=========================================="
echo "Dutch node — synthetic power grid"
echo "=========================================="
echo

for required_file in "$EXPORT_DIR/blueprint.json" "$EXPORT_DIR/dockerinfo.json"; do
    if [ ! -f "$required_file" ]; then
        echo "Missing $required_file" >&2
        echo "Generate it with scripts/onboarding-export-generator.py." >&2
        exit 1
    fi
done

DOCKERINFO=$(cat "$EXPORT_DIR/dockerinfo.json")
BLUEPRINT=$(cat "$EXPORT_DIR/blueprint.json")

# The address the worker will dial comes from the export, not from a constant
# here, so this script cannot drift away from what the orchestrator is told.
SERVICE_HOST=$(json_field "$DOCKERINFO" "docker_info_list.0.ip_address")
SERVICE_PORT=$(json_field "$DOCKERINFO" "docker_info_list.0.port")
SERVICE_NODE=$(json_field "$DOCKERINFO" "docker_info_list.0.container_name")
OPERATION=$(json_field "$BLUEPRINT" "nodes.0.operation_signature_list.0.operation_signature.operation_name")

if [ -z "$SERVICE_HOST" ] || [ -z "$SERVICE_PORT" ]; then
    echo "dockerinfo.json names no service address." >&2
    exit 1
fi

echo "Step 0: pre-flight"
echo "  blueprint node:  $SERVICE_NODE:$OPERATION"
echo "  worker will dial http://$SERVICE_HOST:$SERVICE_PORT"
echo

printf "  orchestrator (%s) ... " "$ORCHESTRATOR_URL"
HEALTH=$(api_get "$ORCHESTRATOR_URL/health" "$ORCHESTRATOR_API_KEY" || true)
if [ "$(json_field "$HEALTH" "status")" = "ok" ]; then
    echo "ok"
else
    echo "UNREACHABLE"
    echo
    echo "  Start it with:  cd orchestrator && docker compose up -d" >&2
    exit 1
fi

# Without a worker the submission succeeds and then nothing happens. Saying so
# here beats a three-minute poll that ends in an unexplained timeout.
printf "  orchestrator workers ... "
if command -v docker >/dev/null 2>&1; then
    WORKER_CONTAINERS=$(docker ps --format '{{.Names}}' \
        --filter "label=com.docker.compose.project=$ORCHESTRATOR_PROJECT" \
        --filter "label=com.docker.compose.service=worker" 2>/dev/null || true)
    WORKER_COUNT=$(printf '%s' "$WORKER_CONTAINERS" | grep -c . || true)

    if [ "${WORKER_COUNT:-0}" -gt 0 ]; then
        echo "$WORKER_COUNT running"
    else
        echo "NONE RUNNING"
        echo
        echo "  A workflow submitted now would be accepted and never picked up." >&2
        echo "  Start them with:  cd orchestrator && docker compose up -d worker" >&2
        exit 1
    fi
    FIRST_WORKER=$(printf '%s' "$WORKER_CONTAINERS" | head -n 1)
else
    echo "unknown (docker not on PATH)"
    FIRST_WORKER=""
fi

# Probe the service from inside a worker: same network, same DNS name, same
# call. Probing from the host would prove nothing, because the service is not
# published there and is not meant to be.
printf "  %s from a worker ... " "$SERVICE_HOST:$SERVICE_PORT"
if [ -n "$FIRST_WORKER" ]; then
    # Reports reachability and how many containers answer to the name, because
    # the second is a failure mode on its own — see below.
    # `|| true` because a bare `VAR=$(cmd)` carries the command's exit status,
    # and under `set -e` a failing `docker exec` — a worker that died since the
    # count above, an image without python — would end the script mid-line with
    # no message at all. An empty PROBE falls through to the UNREACHABLE branch,
    # which is what the operator needs to read.
    PROBE=$(docker exec "$FIRST_WORKER" python -c '
import socket, sys, urllib.request

host, port = sys.argv[1], int(sys.argv[2])

try:
    records = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
except OSError:
    print("unresolved 0 -")
    sys.exit(0)

# Grouped by address family, because one container on a dual-stack network has
# both an A and an AAAA record. Counting records rather than containers would
# refuse a perfectly healthy stack and send the operator hunting for a duplicate
# that does not exist. What matters is how many containers answer, which is the
# largest number of distinct addresses within a single family.
addresses_by_family = {}
for record in records:
    addresses_by_family.setdefault(record[0], set()).add(record[4][0])

addresses = sorted({address for group in addresses_by_family.values() for address in group})
container_count = max((len(group) for group in addresses_by_family.values()), default=0)

try:
    with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=5) as response:
        state = "ok" if response.status == 200 else "unreachable"
except Exception:
    state = "unreachable"

print(state, container_count, ",".join(addresses))
' "$SERVICE_HOST" "$SERVICE_PORT" 2>/dev/null || true)

    PROBE_STATE=$(printf '%s' "$PROBE" | cut -d' ' -f1)
    PROBE_CONTAINER_COUNT=$(printf '%s' "$PROBE" | cut -d' ' -f2)
    PROBE_ADDRESSES=$(printf '%s' "$PROBE" | cut -d' ' -f3)

    if [ "$PROBE_STATE" = "ok" ] && [ "${PROBE_CONTAINER_COUNT:-1}" -gt 1 ]; then
        # Two containers on one network alias round-robin, so the worker starts
        # the job on one and polls the other — which has never heard of the
        # task. The job runs to completion and stores its artifact while the
        # workflow fails with "Task not found", which is about as misleading as
        # a failure gets. Usually a container left behind by an interrupted
        # recreate.
        echo "AMBIGUOUS"
        echo
        echo "  $SERVICE_HOST answers on $PROBE_CONTAINER_COUNT addresses: $PROBE_ADDRESSES" >&2
        echo "  More than one container answers to that name, so Docker balances" >&2
        echo "  between them. The worker would start the job on one and poll the" >&2
        echo "  other, and the workflow would fail with \"Task not found\" while the" >&2
        echo "  job ran to completion somewhere you were not looking." >&2
        echo >&2
        echo "  Find the extra container with:" >&2
        echo "    docker network inspect ai-effect-services" >&2
        exit 1
    fi

    if [ "$PROBE_STATE" = "ok" ]; then
        echo "ok"
    else
        echo "UNREACHABLE"
        echo
        echo "  The worker cannot resolve or reach the address dockerinfo.json names." >&2
        echo "  Start the service with:" >&2
        echo "    cd use-cases/dutch-node-data-synthesizer && docker compose up -d --build" >&2
        echo "  and check it joined the ai-effect-services network." >&2
        exit 1
    fi
else
    echo "skipped"
fi

# A key mismatch used to surface as a failed workflow a submission later: the
# script warned that SERVICE_API_KEY was unset and submitted anyway, and the
# worker's call came back 401. It is knowable here, before anything is spent.
printf "  service api key ... "
if [ -n "$FIRST_WORKER" ]; then
    # `|| true` for the same reason as the probe above: without it a failing
    # `docker exec` ends the script silently, and the "inconclusive" branch
    # below — which exists precisely to tolerate an unanswerable probe — can
    # never be reached. The embedded Python itself always exits 0.
    AUTH_CODE=$(probe_control_auth "$SERVICE_API_KEY" || true)
    AUTH_CODE="${AUTH_CODE:-0}"

    case "$AUTH_CODE" in
        404)
            # The call was authorised; the task id simply does not exist.
            if [ -n "$SERVICE_API_KEY" ]; then
                echo "accepted"
            else
                echo "not required"
            fi
            ;;
        401)
            if [ -z "$SERVICE_API_KEY" ]; then
                echo "REQUIRED BUT UNSET"
                echo
                echo "  This service guards /control/* with a bearer token, so the worker's" >&2
                echo "  call would be rejected and the workflow would fail. Re-run with the" >&2
                echo "  key its container was started with:" >&2
                echo >&2
                echo "    SERVICE_API_KEY=\$(docker exec $SERVICE_HOST printenv SERVICE_API_KEY) \\" >&2
                echo "      ./$(basename "$0")" >&2
            else
                echo "REJECTED"
                echo
                echo "  The service did not accept this SERVICE_API_KEY. It must match the" >&2
                echo "  value its container was started with:" >&2
                echo >&2
                echo "    docker exec $SERVICE_HOST printenv SERVICE_API_KEY" >&2
            fi
            exit 1
            ;;
        *)
            # Not worth failing the run over: the submission itself will say so
            # more precisely than a guess here would.
            echo "inconclusive (HTTP $AUTH_CODE)"
            ;;
    esac
elif [ -z "$SERVICE_API_KEY" ]; then
    echo "unset, unverified (the service must be running open)"
else
    echo "set, unverified"
fi
echo

# ---------------------------------------------------------------------------
# Step 1 — the input
#
# Parameters the synthesizer actually reads. `level_specs` is spelled out rather
# than left to the service's defaults so that what this prints is what it sends.
# ---------------------------------------------------------------------------

echo "Step 1: grid configuration"

SYNTHESIS_INPUT=$("$PYTHON_BIN" - <<'PY'
import base64, json

parameters = {
    "level_specs": [
        {"n": 20, "avg_k": 3.0, "diam": 6, "dist_type": "dgln", "max_k": 15},
        {"n": 60, "avg_k": 2.2, "diam": 10, "dist_type": "dgln", "max_k": 10},
        {"n": 100, "avg_k": 2.0, "diam": 15, "dist_type": "dgln", "max_k": 10},
    ],
    "connection_specs": {
        "(0, 1)": {"type": "k-stars", "c": 0.174, "gamma": 4.15},
        "(1, 2)": {"type": "k-stars", "c": 0.150, "gamma": 4.15},
    },
    "seed": 42,
    "loading_level": "M",
    "ref_sys_id": 1,
}

print(json.dumps(parameters))
print(base64.b64encode(json.dumps(parameters).encode("utf-8")).decode("ascii"))
PY
)
SYNTHESIS_PARAMETERS=$(printf '%s' "$SYNTHESIS_INPUT" | sed -n '1p')
INPUT_B64=$(printf '%s' "$SYNTHESIS_INPUT" | sed -n '2p')

echo "  levels:        3 (20 / 60 / 100 nodes)"
echo "  seed:          42"
echo "  loading_level: M"
echo "  ref_sys_id:    1"
echo

# ---------------------------------------------------------------------------
# Step 2 — submit
# ---------------------------------------------------------------------------

echo "Step 2: submitting to $ORCHESTRATOR_URL/workflows"

PAYLOAD=$("$PYTHON_BIN" - "$EXPORT_DIR" "$INPUT_B64" "$SERVICE_API_KEY" <<'PY'
import json, pathlib, sys

export_directory, encoded_input, service_api_key = sys.argv[1:4]
export_path = pathlib.Path(export_directory)

payload = {
    "blueprint": json.loads((export_path / "blueprint.json").read_text(encoding="utf-8")),
    "dockerinfo": json.loads((export_path / "dockerinfo.json").read_text(encoding="utf-8")),
    "inputs": [{"protocol": "inline", "uri": encoded_input, "format": "json"}],
}

# The orchestrator forwards this to the service as a bearer token. Omitted when
# empty rather than sent blank, so an open service stays open.
if service_api_key:
    payload["services_api_key"] = service_api_key

print(json.dumps(payload))
PY
)

SUBMIT_ARGS=(-s -X POST "$ORCHESTRATOR_URL/workflows" -H "Content-Type: application/json")
if [ -n "$ORCHESTRATOR_API_KEY" ]; then
    SUBMIT_ARGS+=(-H "Authorization: Bearer $ORCHESTRATOR_API_KEY")
fi

RESPONSE=$(printf '%s' "$PAYLOAD" | curl "${SUBMIT_ARGS[@]}" -d @-)
WORKFLOW_ID=$(json_field "$RESPONSE" "workflow_id")

if [ -z "$WORKFLOW_ID" ]; then
    echo "  Submission rejected:"
    json_pretty "$RESPONSE"
    exit 1
fi

echo "  workflow id: $WORKFLOW_ID"
echo

# ---------------------------------------------------------------------------
# Step 3 — wait
# ---------------------------------------------------------------------------

echo "Step 3: waiting for completion"

STATUS=""
POLL_COUNT=0
while [ "$POLL_COUNT" -lt "$MAX_POLLS" ]; do
    STATUS_RESPONSE=$(api_get "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID" "$ORCHESTRATOR_API_KEY" || true)
    STATUS=$(json_field "$STATUS_RESPONSE" "status")

    case "$STATUS" in
        completed|COMPLETED)
            echo "  completed after $((POLL_COUNT * POLL_INTERVAL))s"
            break
            ;;
        failed|FAILED|error|ERROR)
            echo "  failed"
            echo
            # The workflow-level error is often empty; the message that explains
            # what went wrong is on the task.
            echo "  Tasks:"
            json_pretty "$(api_get "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID/tasks" "$ORCHESTRATOR_API_KEY")"
            exit 1
            ;;
        "")
            echo "  no answer from the orchestrator"
            exit 1
            ;;
        *)
            printf "."
            sleep "$POLL_INTERVAL"
            POLL_COUNT=$((POLL_COUNT + 1))
            ;;
    esac
done

if [ "$POLL_COUNT" -ge "$MAX_POLLS" ]; then
    echo
    echo "  Still $STATUS after $((MAX_POLLS * POLL_INTERVAL))s. Last known task state:"
    json_pretty "$(api_get "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID/tasks" "$ORCHESTRATOR_API_KEY")"
    exit 1
fi
echo

# ---------------------------------------------------------------------------
# Step 4 — the result
# ---------------------------------------------------------------------------

echo "Step 4: the result reference"

TASKS_RESPONSE=$(api_get "$ORCHESTRATOR_URL/workflows/$WORKFLOW_ID/tasks" "$ORCHESTRATOR_API_KEY")
TASK_ID=$(json_field "$TASKS_RESPONSE" "tasks.0.task_id")
RESULT_URI=$(json_field "$TASKS_RESPONSE" "tasks.0.output_refs.0.uri")
RESULT_PROTOCOL=$(json_field "$TASKS_RESPONSE" "tasks.0.output_refs.0.protocol")
RESULT_FORMAT=$(json_field "$TASKS_RESPONSE" "tasks.0.output_refs.0.format")
RESULT_DATA_FORMAT=$(json_field "$TASKS_RESPONSE" "tasks.0.output_refs.0.metadata.data_format")

if [ -z "$RESULT_URI" ]; then
    echo "  The task completed but carries no output reference:"
    json_pretty "$TASKS_RESPONSE"
    exit 1
fi

echo "  task:     $TASK_ID"
echo "  protocol: $RESULT_PROTOCOL"
echo "  format:   $RESULT_FORMAT (${RESULT_DATA_FORMAT:-unknown})"
echo "  uri:      $RESULT_URI"
echo

# ---------------------------------------------------------------------------
# Step 5 — fetch it
#
# This is the leg the vendor walks, and the one the old design could not: the
# reference used to name an address only a peer on the Docker network could
# resolve. It is fetched here so a green run means a retrievable result, not
# merely a completed task.
# ---------------------------------------------------------------------------

echo "Step 5: fetching the artifact"

mkdir -p "$OUTPUT_DIR"
RESULT_FILE="$OUTPUT_DIR/$WORKFLOW_ID.json"

FETCH_ARGS=(-s -w '%{http_code}' -o "$RESULT_FILE" "$RESULT_URI")
if [ -n "$SERVICE_API_KEY" ]; then
    FETCH_ARGS=(-s -w '%{http_code}' -o "$RESULT_FILE" -H "Authorization: Bearer $SERVICE_API_KEY" "$RESULT_URI")
fi

# curl prints 000 and exits non-zero when it never got an answer. Both have to
# be caught: `$(curl ... || echo 000)` concatenates the two into "000000".
set +e
HTTP_CODE=$(curl "${FETCH_ARGS[@]}")
CURL_STATUS=$?
set -e
[ "$CURL_STATUS" -ne 0 ] && HTTP_CODE="000"

if [ "$HTTP_CODE" = "000" ]; then
    rm -f "$RESULT_FILE"
    echo "  Could not reach $RESULT_URI."
    echo
    echo "  That URL is built from the service's SELF_URL, which is its public"
    echo "  route through the node's proxy. If the proxy is down, the artifact"
    echo "  still exists — reach it on the internal network instead:"
    echo "    docker exec $SERVICE_HOST curl -s http://localhost:$SERVICE_PORT/control/data/$TASK_ID"
    exit 1
fi

if [ "$HTTP_CODE" != "200" ]; then
    echo "  HTTP $HTTP_CODE fetching the artifact:"
    cat "$RESULT_FILE"
    rm -f "$RESULT_FILE"
    [ "$HTTP_CODE" = "401" ] && echo "  SERVICE_API_KEY does not match the service's."
    exit 1
fi

echo "  written to $RESULT_FILE"
echo

"$PYTHON_BIN" - "$RESULT_FILE" <<'PY'
import json, pathlib, sys

grid = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))

print("  Synthesized grid:")
print(f"    status:       {grid.get('status')}")
print(f"    nodes:        {grid.get('nodes')}")
print(f"    edges:        {grid.get('edges')}")
print(f"    seed:         {grid.get('seed')}")
print(f"    loading:      {grid.get('loading_level')}")
print(f"    grid2op env:  {grid.get('benchmark_env_name')}")
print(f"    pandapower:   {'present' if grid.get('pandapower') else 'absent'}")
PY

echo
echo "=========================================="
echo "Done. Workflow $WORKFLOW_ID"
echo "=========================================="
