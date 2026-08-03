# Human-AI Interaction Testing Service

WP3 Dutch Node service that orchestrates human-operator evaluation sessions using
the [InteractiveAI](https://github.com/AI4REALNET/InteractiveAI) framework and
[hmisurveys](https://github.com/AI4REALNET/hmisurveys). An AI vendor submits a
testing configuration; a human operator opens the grid simulation in a browser
tab and the survey in a second tab; each tool writes its own result file to a
shared directory, and both results are returned to the vendor when the session completes.

## Architecture (v0.2 — Two-Instances Design)

InteractiveAI and hmisurveys run as **independent containers**. The operator
receives two URLs and navigates between them manually. Each tool writes its own
result file; the WP3 service only transitions to COMPLETED when **both** files are present.

```
AI Vendor / Orchestrator
     │
     │  gRPC: StartHumanAISession / GetSessionStatus / GetSessionResult
     ▼
[WP3 hai-testing-service]
     │  Docker Python SDK — launches TWO containers simultaneously
     ├──────────────────────────────────────────────────┐
     ▼                                                  ▼
[InteractiveAI container]                         [hmisurveys container]
 port 8090 → gui_url                              port 8091 → survey_url
 operator runs grid episode                       operator fills survey
 writes kpis.json to /results                     writes survey_outcomes.json to /results
     │                                                  │
     └──────────────┬───────────────────────────────────┘
                    ▼
        [Shared host-mounted results directory]
                    │  polling every 5 s — waits for BOTH files
                    ▼
        [WP3 hai-testing-service]
                    │  both files present → session COMPLETED
                    ▼
             GetSessionResult returns
               kpis + survey_outcomes
```

The operator workflow:
1. Receives `gui_url` (InteractiveAI) — operates the grid until the episode ends.
2. InteractiveAI writes `kpis.json` to the shared directory.
3. Operator opens `survey_url` (hmisurveys) — fills in the questionnaire and submits.
4. hmisurveys writes `survey_outcomes.json` to the shared directory.
5. WP3 detects both files and transitions the session to COMPLETED.

## Submodules

`InteractiveAI/` and `hmisurveys/` are **git submodules** pinned to specific
commits of the AI-EFFECT forks:

| Directory | Submodule URL |
|-----------|---------------|
| `InteractiveAI/` | `https://github.com/AI-EFFECT/InteractiveAI` |
| `hmisurveys/` | `https://github.com/AI-EFFECT/hmisurveys` |

A plain `git clone` leaves both directories **empty**, and
`docker-compose-all.yml` then fails because it builds from `./InteractiveAI/backend`.
Populate them with either:

```bash
git clone --recurse-submodules <this repo>       # fresh clone
git submodule update --init --recursive          # existing clone
```

CI checkouts need the same (for `actions/checkout`, set `submodules: recursive`).

To pull changes from the true upstreams, `cd` into the submodule and use the
`upstream` remote (`ainetus/InteractiveAI`, `AI4REALNET/hmisurveys`), then commit
the updated submodule pointer in this repository.

## Quick Start (Linux)

```bash
# 1. Build the tool images from the submodules (until official images are
#    published — OQ-4). Run from this directory:
# docker build -t interactiveai:latest ./InteractiveAI
# docker build -t hmisurveys:latest ./hmisurveys

# 2. Set required environment variables
export HAI_INTERACTIVE_AI_IMAGE=interactiveai:latest
export HAI_HMISURVEYS_IMAGE=hmisurveys:latest
export HAI_RESULTS_HOST_PATH=/tmp/hai_sessions

# 3. Create the shared results directory and Docker network
mkdir -p /tmp/hai_sessions
docker network create ai-effect-services 2>/dev/null || true

# 4. Start the service
docker compose -f docker-compose-all.yml up --build
```

## Environment Variables

### InteractiveAI Container

| Variable | Default | Description |
|----------|---------|-------------|
| `HAI_INTERACTIVE_AI_IMAGE` | *(required)* | Docker image for InteractiveAI (build locally — see OQ-4) |
| `HAI_INTERACTIVE_AI_PORT` | `8090` | Host port for the InteractiveAI grid GUI |
| `HAI_GUI_BASE_URL` | `http://host.docker.internal:8090` | URL returned as `gui_url`; replace with host IP on Linux |

### hmisurveys Container

| Variable | Default | Description |
|----------|---------|-------------|
| `HAI_HMISURVEYS_IMAGE` | *(required)* | Docker image for hmisurveys (build locally — see OQ-4) |
| `HAI_HMISURVEYS_PORT` | `8091` | Host port for the hmisurveys survey UI |
| `HAI_SURVEY_BASE_URL` | `http://host.docker.internal:8091` | URL returned as `survey_url`; replace with host IP on Linux |

### Shared

| Variable | Default | Description |
|----------|---------|-------------|
| `HAI_RESULTS_HOST_PATH` | `/tmp/hai_sessions` | Absolute host path for session result dirs |
| `HAI_RESULTS_CONTAINER_PATH` | `/hai-sessions` | Mount point inside the service container |
| `HAI_DOCKER_NETWORK` | `ai-effect-services` | Docker network for both sub-containers |
| `HAI_KPIS_FILENAME` | `kpis.json` | Filename written by InteractiveAI on episode end |
| `HAI_SURVEY_FILENAME` | `survey_outcomes.json` | Filename written by hmisurveys on survey submit |
| `GRPC_PORT` | `50051` | gRPC data plane port |
| `PORT` | `8080` | HTTP control plane port |

## Required Volume Mounts

The service container needs two mounts (defined in `docker-compose-all.yml`):

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock   # Docker socket for container management
  - /tmp/hai_sessions:/hai-sessions             # Shared results directory (both tools write here)
```

## gRPC API

**StartHumanAISession** — Start a session, receive two browser URLs.

```
StartHumanAISession(HumanAISessionSpec) → StartSessionResponse
  HumanAISessionSpec:
    scenario.name              (str, empty = InteractiveAI default)
    agent.name                 (str, empty = InteractiveAI built-in agent)
    survey.survey_id           (str, empty = hmisurveys default)
    kpis                       (list[str], empty = collect all from InteractiveAI)
    session_timeout_seconds    (int, > 0, required)

  StartSessionResponse:
    session_id    (str) — stable identifier for polling
    gui_url       (str) — open in browser: InteractiveAI grid simulation
    survey_url    (str) — open in browser after episode: hmisurveys questionnaire
```

**GetSessionStatus** — Poll until COMPLETED or FAILED.

```
Phases: PENDING → GUI_READY → IN_PROGRESS → SURVEY → COMPLETED | FAILED

COMPLETED requires: kpis.json AND survey_outcomes.json both present.
FAILED if timeout expires before both files appear.
```

**GetSessionResult** — Fetch results after COMPLETED.

```
Returns:
  kpis            map<string, MetricValue>  — from InteractiveAI kpis.json
  survey_outcomes map<string, MetricValue>  — from hmisurveys survey_outcomes.json
Error if session is not yet COMPLETED.
```

## Windows Development — Known Issues

> **InteractiveAI and hmisurveys compatibility on Windows is limited.**

**Recommended workaround:** Use WSL2 or a Linux VM for development and testing.

- `host.docker.internal` resolves correctly on Docker Desktop (Windows/Mac) but
  must be replaced with the host IP on native Linux deployments.
- The Docker socket path `/var/run/docker.sock` requires Docker Desktop WSL integration
  to be enabled on Windows.

This is a **known v1 limitation** (NFR-01). The primary deployment target is Linux.

## Unresolved Items Before End-to-End Use

### OQ-1 — InteractiveAI results export hook

InteractiveAI must write `kpis.json` to `/results` (the mounted volume path) on
grid episode end. This is not yet implemented upstream. Required format:

```json
{ "<kpi_name>": <value>, ... }
```

Env vars the container will receive: `HAI_RESULTS_PATH=/results`, `HAI_KPIS_FILENAME=kpis.json`.
The WP3 service reads `kpis.json` dynamically — keys are not hardcoded.

### OQ-2 — hmisurveys results export hook

hmisurveys must write `survey_outcomes.json` to `/results` on survey submit.
Required format:

```json
{ "<field_name>": <value>, ... }
```

Env vars the container will receive: `HAI_RESULTS_PATH=/results`, `HAI_SURVEY_FILENAME=survey_outcomes.json`.

> **Until OQ-1 and OQ-2 are resolved, the polling loop has nothing to pick up
> and the service cannot complete a session.**

### OQ-3 — KPI names and survey field names

Both are tool-dependent. The WP3 service parses keys dynamically — no code
change is needed once the export hooks are in place.

### OQ-4 — Docker image names

Build locally from:
- [AI4REALNET/InteractiveAI](https://github.com/AI4REALNET/InteractiveAI) → `HAI_INTERACTIVE_AI_IMAGE`
- [AI4REALNET/hmisurveys](https://github.com/AI4REALNET/hmisurveys) → `HAI_HMISURVEYS_IMAGE`

### OQ-5 — InteractiveAI web-app internal port

Unknown until the image is built and run. Set `HAI_INTERACTIVE_AI_PORT` once confirmed.
The current default (`8080/tcp` inside the container, mapped to `8090` on the host)
mirrors a typical Flask/FastAPI app — adjust if different.

### OQ-6 — Docker socket in deployment

Needs verification that `/var/run/docker.sock` is accessible inside the container.
Mount it explicitly (see `docker-compose-all.yml`).

### OQ-7 — hmisurveys internal port

Unknown until the image is built and run. Set `HAI_HMISURVEYS_PORT` once confirmed.

## Known v1 Limitations

- **Single session at a time**: only one active session per service instance.
- **No session persistence**: a service restart loses all in-progress session state.
- **No GUI authentication**: `gui_url` and `survey_url` are publicly accessible to
  anyone with the URL during the session lifetime.
- **Manual operator handoff**: the operator must navigate from `gui_url` to `survey_url`
  themselves — the two tools are not yet integrated.

## Running Tests

```bash
cd use-cases/dutch-node-hai-testing/services/human_ai_interaction_testing
pip install -r requirements.txt
pytest tests/ -v
```

Tests requiring a compiled proto are automatically skipped if `grpcio-tools` is
not installed. Docker-dependent tests (container launch) are excluded from the
unit test suite; run them as integration tests once OQ-4 and OQ-6 are resolved.
