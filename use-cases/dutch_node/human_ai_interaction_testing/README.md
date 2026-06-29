# Human-AI Interaction Testing Service

WP3 Dutch Node service that orchestrates human-operator evaluation sessions using
the [InteractiveAI](https://github.com/AI4REALNET/InteractiveAI) framework and
[hmisurveys](https://github.com/AI4REALNET/hmisurveys). An AI vendor submits a
testing configuration; a human operator runs a grid2op simulation via browser GUI;
after the session, a survey is shown automatically and results are returned to the vendor.

## Architecture

```
AI Vendor / Orchestrator
     │
     │  gRPC: StartHumanAISession / GetSessionStatus / GetSessionResult
     ▼
[WP3 hai-testing-service]
     │  Docker Python SDK
     ▼
[InteractiveAI container]  ──browser──►  Human Operator
     │  writes results JSON on survey submit
     ▼
[Shared results directory]
     │  polling (5s interval)
     ▼
[WP3 hai-testing-service]  →  session transitions to COMPLETED
```

## Quick Start (Linux)

```bash
# 1. Set required environment variables
export HAI_INTERACTIVE_AI_IMAGE=interactiveai:latest   # TODO: confirm image name
export HAI_RESULTS_HOST_PATH=/tmp/hai_sessions

# 2. Create the shared results directory
mkdir -p /tmp/hai_sessions

# 3. Create the Docker network (if not already present)
docker network create ai-effect-services 2>/dev/null || true

# 4. Start the service
docker compose -f docker-compose-all.yml up --build
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HAI_INTERACTIVE_AI_IMAGE` | *(required)* | Docker image for InteractiveAI (see OQ-4) |
| `HAI_INTERACTIVE_AI_PORT` | `8090` | Host port for the InteractiveAI GUI |
| `HAI_GUI_BASE_URL` | `http://host.docker.internal:8090` | URL returned to callers as `gui_url` |
| `HAI_RESULTS_HOST_PATH` | `/tmp/hai_sessions` | Absolute host path for session result dirs |
| `HAI_RESULTS_CONTAINER_PATH` | `/hai-sessions` | Mount point inside the service container |
| `HAI_DOCKER_NETWORK` | `ai-effect-services` | Docker network for InteractiveAI containers |
| `HAI_RESULTS_FILENAME` | `session_result.json` | Filename written by InteractiveAI on submit |
| `GRPC_PORT` | `50051` | gRPC data plane port |
| `PORT` | `8080` | HTTP control plane port |

## Required Volume Mounts

The service container needs two mounts (defined in `docker-compose-all.yml`):

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock      # Docker socket for container management
  - /tmp/hai_sessions:/hai-sessions                # Shared results directory
```

## gRPC API

**StartHumanAISession** — Start a session and get a browser URL.

```
StartHumanAISession(HumanAISessionSpec) → StartSessionResponse
  HumanAISessionSpec:
    scenario.name              (str, empty = default)
    agent.name                 (str, empty = built-in)
    survey.survey_id           (str, empty = default)
    kpis                       (list[str], empty = collect all)
    session_timeout_seconds    (int, > 0, required)
```

**GetSessionStatus** — Poll until COMPLETED or FAILED.

```
Phases: PENDING → GUI_READY → IN_PROGRESS → SURVEY → COMPLETED | FAILED
```

**GetSessionResult** — Fetch results after COMPLETED.

```
Returns: kpis (map<string, MetricValue>), survey_outcomes (map<string, MetricValue>)
Error if session is not yet COMPLETED.
```

## Windows Development — Known Issues

> **InteractiveAI compatibility on Windows is limited.** Expect startup failures
> when running the InteractiveAI container on Windows hosts.

**Recommended workaround:** Use WSL2 or a Linux VM for development and testing.

Specific issues observed:
- InteractiveAI may fail to start due to file path or display issues on Windows.
- The `host.docker.internal` hostname works on Docker Desktop (Windows/Mac) but
  must be replaced with the host IP on native Linux deployments.
- The Docker socket path `/var/run/docker.sock` is valid on Linux and Docker Desktop
  but requires the Docker Desktop WSL integration to be enabled on Windows.

This is a **known v1 limitation** (NFR-01). The primary development and deployment
target is Linux.

## Unresolved Items Before End-to-End Use

### OQ-1 — InteractiveAI ↔ hmisurveys integration (MAJOR BLOCKER)

InteractiveAI and hmisurveys are **not yet connected**. Two pieces of work are
needed inside the InteractiveAI layer (this is development work outside WP3):

1. **Survey trigger**: after the grid episode ends, show the hmisurveys survey
   in the same browser tab automatically.
2. **Results export**: after the operator submits the survey, collect grid KPIs
   from InteractiveAI and survey outcomes from hmisurveys and write them as:
   ```json
   {
     "kpis": { "<kpi_name>": <value>, ... },
     "survey_outcomes": { "<field_name>": <value>, ... }
   }
   ```
   to `/results/session_result.json` (the mounted volume path).

**Until OQ-1 is resolved, the WP3 polling loop has nothing to pick up and the
service cannot complete a session.**

### OQ-2 — Survey field names

Depend on which hmisurveys survey is chosen. Determine once a survey is selected.
The WP3 service parses keys dynamically so no code change is needed — only
documentation and test fixtures.

### OQ-3 — InteractiveAI KPI names

Unknown until InteractiveAI is run. Investigate the InteractiveAI source for KPI
emission. Again, no WP3 code change needed — keys are parsed dynamically.

### OQ-4 — Docker image name ✅

Build locally from [AI4REALNET/InteractiveAI](https://github.com/AI4REALNET/InteractiveAI).
Set `HAI_INTERACTIVE_AI_IMAGE` to the local image tag after building.

### OQ-5 — InteractiveAI web-app port

Unknown. Run InteractiveAI locally and check which port it serves on, then set
`HAI_INTERACTIVE_AI_PORT` and `HAI_GUI_BASE_URL` accordingly.

### OQ-6 — Docker socket in deployment

Needs verification. If the socket is not accessible, the service cannot manage
containers. Mount `/var/run/docker.sock` explicitly (see `docker-compose-all.yml`).

## Known v1 Limitations

- **Single session at a time**: only one active session per service instance.
- **No session persistence**: a service restart loses all in-progress session state.
- **No GUI authentication**: the `gui_url` is publicly accessible to anyone with the URL.

## Running Tests

```bash
cd use-cases/dutch_node/human_ai_interaction_testing
pip install -r requirements.txt
pytest tests/ -v
```

Tests that require a compiled proto (`_metric_value_from_any` tests) are automatically
skipped if `grpcio-tools` is not installed or the proto has not been compiled yet.
Docker-dependent tests (full session lifecycle) are not in the unit test suite;
run them manually as integration tests once OQ-4 and OQ-6 are resolved.
