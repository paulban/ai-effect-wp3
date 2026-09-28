# Dutch Node — Human-AI Interaction Testing

A human-in-the-loop evaluation of an AI-assisted power-grid operator workflow.
A session hands a participant two browser links — a grid simulator and a
questionnaire — collects what they produce, and returns it as a single result.

**Spec:** `.claude/specs/dutch-node-target-architecture-spec.md`

---

## How a session works

```
orchestrator ──POST /control/execute──▶ hai-testing-service
                                             │
                                    reserve a free slot (Redis, SET NX)
                                             │
                                    configure it over HTTP
                                             │
                    ◀── gui_url + survey_url, signed and proxied ──
                                             │
participant ──https://<node>/s/<id>/gui?t=…──▶ session-proxy
                                             │  validates the token,
                                             │  asks the control service
                                             │  which slot serves <id>
                                             ▼
                                    hai-slot-<n>-simulator
                                    hai-slot-<n>-survey
                                             │
                       results POSTed back to /wp3/collect/…
                                             │
                              both arrived ──▶ session COMPLETED,
                                               artifact stored,
                                               slot released
```

The orchestrator polls `/control/status/{task_id}` and reads the result from
`/control/output/{task_id}`, which returns an HTTP URL it can fetch.

## What is deliberately absent

- **No Docker socket.** Session containers are declared in `docker-compose.yml`
  and stay up. Starting a session reserves one and configures it; nothing
  creates containers at runtime.
- **No published ports except the proxy.** The control service, Redis and every
  slot are reachable only on the internal network.
- **No polling thread and no shared results directory.** Results are posted to
  the service; the second one to arrive completes the session.
- **No gRPC or protobuf.** The service runs standalone, not as a pipeline
  node. The results data plane is the HTTP artifact endpoint.

## Running it

The InteractiveAI CAB platform is a separate, long-lived stack. Start it first;
this compose file attaches to the same `ai-effect-services` network and reaches
it by name.

```bash
docker network create ai-effect-services   # if it does not exist

export HAI_SESSION_TOKEN_SECRET=$(openssl rand -hex 32)
export HAI_PUBLIC_BASE_URL=https://node.example.org   # as a participant sees it
export SERVICE_API_KEY=$(openssl rand -hex 32)

cd use-cases/dutch-node-hai-testing
docker compose up -d --build
```

Both `HAI_SESSION_TOKEN_SECRET` and `HAI_PUBLIC_BASE_URL` are required and the
stack refuses to start without them. The first signs participant links; the
second is what those links point at, so an internal Docker name here produces
links no participant can open.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `HAI_PROXY_HOST_PORT` | `8443` | The only published port |
| `HAI_SESSION_SLOT_COUNT` | `2` | Concurrent sessions. Must match the number of slot pairs in the compose file |
| `HAI_SESSION_TOKEN_SECRET` | — | **Required.** Signs session links |
| `HAI_PUBLIC_BASE_URL` | — | **Required.** Base URL participants reach the node at |
| `SERVICE_API_KEY` | unset | Bearer token on `/control/*`. Unset means open — acceptable only on a developer machine |
| `HAI_CAB_URL` | `http://frontend:80` | CAB platform address passed to each simulator |

### Adding a third concurrent participant

Add a `hai-slot-3-simulator` / `hai-slot-3-survey` pair to
`docker-compose.yml` following the existing ones, and raise
`HAI_SESSION_SLOT_COUNT` to 3. No firewall change is involved, because slots
publish no ports. The binding constraint is memory: each idle simulator holds a
loaded grid2op environment.

## Submodule

The simulator image is built from the `feat/wp3-simulator-config-api` branch of
[`AI-EFFECT/InteractiveAI`](https://github.com/AI-EFFECT/InteractiveAI), which
adds environment-based configuration and the `/hai/session`, `/hai/reset` and
`/hai/state` endpoints this service drives. The branch is additive: with no WP3
variables set and no `/hai` call, the app behaves exactly as upstream.

`hmisurveys` is unchanged.

## Tests

```bash
cd services/human_ai_interaction_testing
python -m pytest tests/ -q
```

34 tests cover the acceptance criteria in section 9.1 of the spec: concurrent
slot reservation, exhaustion, timeout reclamation, token forgery and expiry,
unauthenticated control calls, ambiguous result attribution, and event-driven
completion.

**Not covered by these tests**, because they need a running stack: the browser
round trip, the real HTTP configuration of a slot, and whether memory is
actually reclaimed when a simulator is reused across sessions (open question 1
in the spec).
