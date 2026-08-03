# AI-Effect Orchestrator

REST API and worker processes for workflow orchestration.

## Quick Start

```bash
docker compose up -d
```

This starts:
- **redis** - State management (port 16379 externally, 6379 internally)
- **api** - REST API (port 18000 externally, 8000 internally)
- **worker** - 3 worker replicas for task execution

## Verify Running

```bash
# Check containers
docker compose ps

# Check health
curl -s http://localhost:18000/health | jq .
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/workflows` | POST | Submit workflow |
| `/workflows/{id}` | GET | Get workflow status |
| `/workflows/{id}/tasks` | GET | Get workflow tasks |

## Submit a Workflow

```bash
curl -s -X POST http://localhost:18000/workflows \
  -H "Content-Type: application/json" \
  -d '{
    "blueprint": {...},
    "dockerinfo": {...},
    "inputs": [{"protocol": "inline", "uri": "...", "format": "json"}]
  }' | jq .
```

## Service Network

Workers automatically join the `ai-effect-services` Docker network to reach services by DNS name.

The network is auto-created by the use case `start.sh` scripts and the root-level `./start.sh` convenience script. If starting the orchestrator first, create it manually:

```bash
docker network create ai-effect-services
```

Services declare this network in their docker-compose:

```yaml
networks:
  default:
    name: ai-effect-services
    external: true
```

To verify connectivity:
```bash
docker network inspect ai-effect-services --format '{{range .Containers}}{{.Name}} {{end}}'
```

## Logs

```bash
# All logs
docker compose logs -f

# API only
docker compose logs -f api

# Workers only
docker compose logs -f worker
```

## Stop

```bash
docker compose down
```

## Development

### Run Tests

```bash
# Unit tests
./run-tests-unit.sh

# Integration tests (requires Docker)
./run-tests-integration.sh

# All tests
./run-tests.sh
```

### Run Locally (without Docker)

```bash
# Install dependencies
pip install -r requirements.txt

# Start Redis (required)
docker run -d --name redis -p 6379:6379 redis:7-alpine

# Start API
REDIS_URL=redis://localhost:6379 python -m src.main

# Start worker (separate terminal)
REDIS_URL=redis://localhost:6379 python -m src.worker_daemon
```

## Using with the Portal

The orchestrator is the execution backend for the [AI-Effect WP3 Portal](https://portal.renewenergy.io). The portal submits workflows via the REST API and polls for status. Two common setups:

### Local orchestrator + ngrok (testing)

Run the orchestrator locally and expose it to the portal via ngrok:

```bash
# Start orchestrator
ORCHESTRATOR_API_KEY=your-key docker compose up -d

# Expose via ngrok
ngrok http 18000
# → copy the https://xxx.ngrok-free.app URL
```

Set the ngrok URL as the **Orchestrator URL** on the solution in the portal (either in the solution Edit page or directly in the deploy dialog).

> **ngrok is for testing only.** The free tier gives a different URL each restart. For a stable deployment, use a reverse proxy instead — see below.

### Server orchestrator (stable)

Deploy the orchestrator on a server and expose it via a reverse proxy with HTTPS. No code changes are needed — the orchestrator serves plain HTTP; TLS termination happens at the proxy layer (nginx, Traefik, Caddy all work).

```bash
# On your server:
ORCHESTRATOR_API_KEY=your-key docker compose up -d
# Then configure your reverse proxy to forward HTTPS → localhost:18000
```

Set `https://your-orchestrator.example.com` as the Orchestrator URL in the portal. The portal can also be configured with a server-side default (`ORCHESTRATOR_URL` env var) so solutions don't need the URL set explicitly.

The same HTTPS-via-proxy approach applies to the pipeline services (each runs on port `8080` internally).

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://redis:6379` | Redis connection URL |
| `HOST` | `0.0.0.0` | API bind host |
| `PORT` | `8000` | API bind port |
| `WORKER_POLL_INTERVAL` | `1.0` | Worker task poll interval (seconds) |

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│    Client   │────▶│     API     │────▶│    Redis    │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                               │
                         ┌─────────────────────┼─────────────────────┐
                         │                     │                     │
                         ▼                     ▼                     ▼
                  ┌─────────────┐       ┌─────────────┐       ┌─────────────┐
                  │  Worker 1   │       │  Worker 2   │       │  Worker 3   │
                  └─────────────┘       └─────────────┘       └─────────────┘
                         │                     │                     │
                         └─────────────────────┼─────────────────────┘
                                               │
                                               ▼
                                        ┌─────────────┐
                                        │  Services   │
                                        └─────────────┘
```

- **API**: Receives workflow submissions, stores in Redis
- **Redis**: Workflow state, task queue, results
- **Workers**: Poll for tasks, execute by calling services, update state
