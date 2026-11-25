# HAI Public Access (ports 443 / 8443) Spec

**Status:** Draft
**Date:** 2026-11-25
**Domain:** Architecture (deployment configuration)
**Tier:** B — Focused spec
**Related:** `hai-orchestrated-session-spec.md` (FR-22 to FR-25: `setup_interactiveai.sh`)

---

## 1. Overview

The HAI session stack on `aieffect-dutch01.ewi.tudelft.nl` is only usable on
the server itself: the InteractiveAI frontend answers on `localhost:3200`, the
PowerGrid simulator on `localhost:5122`, and SSH tunnels are not an option for
participants. TU Delft ICT has opened ports **443** and **8443** on the server
to the TU Delft internal network. This spec makes the stack reachable there,
over plain HTTP, as an opt-in server configuration:

- InteractiveAI: `http://aieffect-dutch01.ewi.tudelft.nl:443`
- PowerGrid simulator: `http://aieffect-dutch01.ewi.tudelft.nl:8443`

Machines without the opt-in (developer laptops) behave exactly as today.

---

## 2. Goals

1. From a laptop on the TU Delft network (or VPN), `http://aieffect-dutch01.ewi.tudelft.nl:443`
   opens InteractiveAI and the test account `powergrid_user` / `test` logs in.
2. From the same laptop, `http://aieffect-dutch01.ewi.tudelft.nl:8443` opens the
   PowerGrid simulator; its login (server `http://host.docker.internal:3200/`)
   and dashboard work.
3. One server-only file switches this on; nothing else needs editing by hand.
4. The participant instructions printed by `submit-workflow.sh` show the two
   public URLs when public access is on.
5. Without the opt-in, nothing binds 443 or 8443 and all URLs stay `localhost`.

## 3. Non-Goals

- HTTPS / TLS, certificates. Port 443 serves **plain HTTP** in this version.
- Access from outside the TU Delft network without VPN (ICT's firewall).
- Changing or removing the internal ports 3200 (frontend) and 5122 (simulator).
- Per-participant accounts or any change to authentication beyond Keycloak's Frontend URL.
- Changes to the InteractiveAI submodule (it stays unmodified, as today).
- Exposing Keycloak (stays on `127.0.0.1:16201`), the orchestrator (`127.0.0.1:18000`) or `hai-session-runner`.
- A fully played session as an acceptance criterion (see 9.3).

---

## 5. Requirements

### 5.1 Functional Requirements

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-01 | A file `use-cases/dutch-node-hai-testing/.env` (gitignored) configures public access with three keys: `HAI_PUBLIC_HOST` (no default; unset or blank = public access off), `HAI_FRONTEND_PUBLIC_PORT` (default `443`), `HAI_SIMULATOR_PUBLIC_PORT` (default `8443`). A committed `.env.example` documents them, with the TU Delft values commented in. | Must | Compose reads `.env` from the project directory automatically, so `hai-session-runner` gets the same values. |
| FR-02 | Public access is **off** when `HAI_PUBLIC_HOST` is unset or blank: no extra ports, all URLs and Keycloak's Frontend URL as today. | Must | Default on every developer machine. |
| FR-03 | When on, the `frontend` container publishes `0.0.0.0:${HAI_FRONTEND_PUBLIC_PORT}:80` **in addition to** `3200:80`. | Must | New override file `public-frontend.override.yml`, applied to the backend stack only when public access is on. No `!override`: the list is appended to. |
| FR-04 | When on, `PowerGrid-simulator-app` publishes `0.0.0.0:${HAI_SIMULATOR_PUBLIC_PORT}:5000` **in addition to** `5122:5000`. | Must | New override file `public-simulator.override.yml`, applied after `simulator.override.yml` only when public access is on. |
| FR-05 | `setup_interactiveai.sh` reads the three keys from `.env` by parsing `KEY=value` lines (comments, blank lines, surrounding quotes and whitespace ignored). It never `source`s the file. A variable already set in the shell environment overrides the file. | Must | |
| FR-06 | When on, the setup script's frontend URL (`FRONTEND_URL`, used for Keycloak's Frontend URL, its health checks and the test login) is `http://${HAI_PUBLIC_HOST}:${HAI_FRONTEND_PUBLIC_PORT}`. Precedence: explicit `HAI_FRONTEND_PUBLIC_URL` > derived public URL > `http://localhost:3200`. | Must | Login then works only at the public URL — accepted. |
| FR-07 | `hai-session-runner` derives its participant links with the same rule: `frontend_public_url` = `HAI_FRONTEND_PUBLIC_URL` if set, else `http://${HAI_PUBLIC_HOST}:${HAI_FRONTEND_PUBLIC_PORT or 443}` if the host is set, else `http://localhost:3200`. `simulator_public_url` likewise with `HAI_SIMULATOR_PUBLIC_URL`, `HAI_SIMULATOR_PUBLIC_PORT` (default `8443`) and local default `http://localhost:5122`. | Must | In `settings.py`; `docker-compose.yml` passes the five variables through. |
| FR-08 | Port values must be integers 1–65535. When public access is on, the frontend and simulator public ports must differ from each other and from 3200, 5122 and `KC_PORT`. The setup script exits with a clear error otherwise; `HAISettings.from_environment` raises `ValueError` for a non-integer or out-of-range port. | Must | |
| FR-09 | When on, before starting containers the setup script checks each public port. If something is listening on it that is not this stack's container (`frontend` for the frontend port, `PowerGrid-simulator-app` for the simulator port), the script exits with an error that names the port. If `ss` is not available, it warns and skips the check. | Should | Check with `ss -Hltn "sport = :<port>"` and `docker ps --filter publish=<port> --format '{{.Names}}'`. |
| FR-10 | When on, step 6 (`verify`) also checks from the server that `http://${HAI_PUBLIC_HOST}:${HAI_SIMULATOR_PUBLIC_PORT}/` returns 200. A failure is a warning, not an error. The frontend URL is already checked in step 2. | Should | |
| FR-11 | `print_summary` prints the public URLs when on, plus a line saying they are reachable only from the TU Delft network or VPN. | Should | |
| FR-12 | Turning public access off again (removing or blanking `HAI_PUBLIC_HOST`) and re-running the setup releases 443 and 8443 and restores the localhost Frontend URL. | Must | Falls out of FR-02 to FR-04: `up -d` recreates `frontend` when its ports change; the simulator is always `--force-recreate`d. |
| FR-13 | The use case README gains a short "Public access" section: what `.env` does, the two URLs, the localhost-login trade-off, and how to switch it off. | Should | |

---

## 6. Assumptions & Constraints

- **Assumption:** ICT forwards TCP 443 and 8443 from the TU Delft network to the server unchanged (no TLS termination, no port rewrite). Confirmed by the user ("ports already open internally").
- **Assumption:** Docker on the server is rootful, so binding ports below 1024 works. The `docker-proxy` processes in `ss` output on the server indicate rootful Docker.
- **Assumption:** On the server, `aieffect-dutch01.ewi.tudelft.nl` resolves to an address of the server itself, so the setup script's own checks through the public URL work.
- **Constraint:** The InteractiveAI submodule stays unmodified; everything goes through override files and environment variables, as for Keycloak (`keycloak.override.yml`).
- **Constraint:** Keycloak accepts tokens for one Frontend URL. With public access on, login at `localhost:3200` stops working (accepted by the user).
- **Constraint:** The simulator's server-side connection to InteractiveAI (`http://host.docker.internal:3200/`) and the frontend's `/powergrid-simu/` proxy (`host.docker.internal:5122`) stay on the internal ports. That is why FR-03 and FR-04 add ports rather than replace them.

---

## 8. Open Questions

None blocking implementation. One item to confirm during verification, not before:

| # | Question | Owner | Due |
|---|----------|-------|-----|
| 1 | Does Keycloak's `opfab-client` need the public URL in its Valid Redirect URIs? The GUI's password-grant login (`/auth/token`) does not use redirects, so it is expected not to. If AC-2 fails with a redirect error, add `http://<host>:443/*` there; the setup script's manual fallback already describes the step. | Implementer | During AC-2 |

---

## 9. Verification

### 9.1 Acceptance Criteria

- **AC-1 (FR-01, FR-03, FR-04, FR-05):** **Given** `.env` with `HAI_PUBLIC_HOST=aieffect-dutch01.ewi.tudelft.nl` on the server, **when** `scripts/setup_interactiveai.sh` runs, **then** it finishes without errors and `docker ps --format '{{.Names}} {{.Ports}}'` shows `frontend` with both `0.0.0.0:443->80/tcp` and `0.0.0.0:3200->80/tcp`, and `PowerGrid-simulator-app` with both `0.0.0.0:8443->5000/tcp` and `0.0.0.0:5122->5000/tcp`.
- **AC-2 (Goal 1, FR-06):** **Given** AC-1, **when** a laptop on the TU Delft network opens `http://aieffect-dutch01.ewi.tudelft.nl:443` and logs in as `powergrid_user` / `test`, **then** the InteractiveAI GUI loads after login.
- **AC-3 (Goal 2, FR-04):** **Given** AC-1, **when** the laptop opens `http://aieffect-dutch01.ewi.tudelft.nl:8443` and logs in with server `http://host.docker.internal:3200/`, **then** the dashboard opens and its live graph connects (no Socket.IO errors in the browser console).
- **AC-4 (Goal 4, FR-07):** **Given** the same `.env` and `hai-session-runner` (re)started with `docker compose up -d --build` in `use-cases/dutch-node-hai-testing`, **when** `bash submit-workflow.sh` reaches step 3, **then** the printed instructions contain `http://aieffect-dutch01.ewi.tudelft.nl:8443` and `http://aieffect-dutch01.ewi.tudelft.nl:443`.
- **AC-5 (FR-02):** **Given** no `.env` (a developer laptop), **when** the setup script runs, **then** nothing listens on 443 or 8443, Keycloak's Frontend URL is `http://localhost:3200`, and `hai-session-runner`'s links are `http://localhost:3200` and `http://localhost:5122`.
- **AC-6 (FR-12):** **Given** AC-1, **when** `HAI_PUBLIC_HOST` is blanked and the setup script re-runs, **then** 443 and 8443 are released and AC-5's state holds.
- **AC-7 (FR-08):** **Given** `HAI_FRONTEND_PUBLIC_PORT=3200` (or `abc`, or `70000`), **when** the setup script runs, **then** it exits non-zero before starting any container, naming the offending variable. **Given** `HAI_PUBLIC_HOST` set and `HAI_FRONTEND_PUBLIC_PORT=abc`, **when** `HAISettings.from_environment` runs, **then** it raises `ValueError` naming `HAI_FRONTEND_PUBLIC_PORT`.
- **AC-8 (FR-05):** **Given** `.env` with `HAI_PUBLIC_HOST=a` and the shell exporting `HAI_PUBLIC_HOST=b`, **when** the setup script runs, **then** it uses `b`.

### 9.2 Test Scenarios

| Scenario | Input / State | Expected Result | Covers |
|----------|---------------|-----------------|--------|
| Settings: public off | env without `HAI_PUBLIC_HOST` | links `http://localhost:3200`, `http://localhost:5122` | FR-07, FR-02 |
| Settings: public on, default ports | `HAI_PUBLIC_HOST=h` | `http://h:443`, `http://h:8443` | FR-07 |
| Settings: custom ports | `HAI_PUBLIC_HOST=h`, ports `8080`/`9090` | `http://h:8080`, `http://h:9090` | FR-07 |
| Settings: explicit URL wins | `HAI_PUBLIC_HOST=h`, `HAI_FRONTEND_PUBLIC_URL=http://x` | frontend `http://x`, simulator `http://h:8443` | FR-07 |
| Settings: blank host | `HAI_PUBLIC_HOST="  "` | treated as off | FR-02 |
| Settings: bad port | `HAI_PUBLIC_HOST=h`, `HAI_SIMULATOR_PUBLIC_PORT=0` | `ValueError` naming the variable | FR-08 |
| Script: `.env` parsing | file with comments, blank lines, `KEY="value"`, `KEY = value` | values parsed, quotes and whitespace stripped | FR-05 |
| Script: shell overrides file | file `a`, shell `b` | `b` | FR-05 |
| Script: port clash | `HAI_FRONTEND_PUBLIC_PORT=5122` | exits non-zero before `compose up` | FR-08 |
| Script: port taken by a foreign process | something else listening on 443 | exits non-zero naming 443 | FR-09 |
| Compose merge | `config` of backend files + `public-frontend.override.yml` | `frontend` ports contain both 3200 and 443 mappings | FR-03 |
| Compose merge | `config` of simulator files + `public-simulator.override.yml` | `app` ports contain both 5122 and 8443 mappings | FR-04 |
| Server: end to end | AC-1 to AC-4 on `aieffect-dutch01` | as stated | Goals 1–4 |

### 9.3 Definition of Done

- [ ] All Must requirements implemented
- [ ] Unit tests for FR-07 / FR-08 in `tests/test_settings.py` pass, and the existing test suite still passes
- [ ] `bash -n` passes on the setup script; compose `config` merges checked as in 9.2
- [ ] AC-1 to AC-5 verified on `aieffect-dutch01` from a laptop on the TU Delft network (a fully played session is **not** required for v1)
- [ ] `.env.example` committed; `.env` gitignored
- [ ] README "Public access" section written

---

## 10. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Plain HTTP on 443: browsers that auto-upgrade, or a user typing the host without `http://`, try HTTPS and fail | Med | Low | Always hand out the full `http://…:443` URL (the participant instructions do). HTTPS is future work. |
| Both UIs are open to the whole TU Delft network, with the shared `powergrid_user` / `test` login and no encryption | Med | Med | Internal network only; switch off between studies (FR-12); per-participant accounts and HTTPS are future work. |
| Keycloak rejects logins from the public URL (issuer or redirect mismatch) | Low | High | FR-06 sets the Frontend URL; Open Question 1 covers redirect URIs. |
| The server cannot reach its own public hostname, so the setup checks fail | Low | Low | Step 2 errors would show it; check `getent hosts aieffect-dutch01.ewi.tudelft.nl` on the server. |
| The simulator page loads Socket.IO from `cdn.socket.io`, so the live graph fails without internet access | Low | Med | The TU Delft network has internet access; noted in the README. |
| Another service on the server already uses 443 or 8443 | Low | High | FR-09 fails early with the port named; ports are configurable (FR-01). |

---

## 11. Revision History

| Version | Date | Author | Summary of changes |
|---------|------|--------|-------------------|
| 0.1 | 2026-11-25 | — | Initial draft |

---

## 12. Implementation Handoff

> This section is the single source of truth for the implementation agent.
> Every decision needed to start coding is captured here.

**Language & runtime:** Bash (the setup script; must run under bash on Linux, the server, and Git Bash on Windows, developer laptops), Docker Compose v2+ YAML (server: Compose v5.7.2), Python 3.11 (`hai-session-runner`, `settings.py`).
**Execution model:** Synchronous shell script; Python settings are read once at service start (`HAISettings.from_environment`).
**Entry point:** `use-cases/dutch-node-hai-testing/scripts/setup_interactiveai.sh` → `main()`.
**Code location** (all under `use-cases/dutch-node-hai-testing/`):
- `scripts/setup_interactiveai.sh`: extend.
- `public-frontend.override.yml`: **new**. `services: frontend: ports: ["0.0.0.0:${HAI_FRONTEND_PUBLIC_PORT:-443}:80"]`, no `!override`, header comment in the style of `keycloak.override.yml`.
- `public-simulator.override.yml`: **new**. `services: app: ports: ["0.0.0.0:${HAI_SIMULATOR_PUBLIC_PORT:-8443}:5000"]`, no `!override`.
- `.env.example`: **new**. The three keys, commented TU Delft values, one line per key explaining it.
- `.gitignore`: add `.env`.
- `docker-compose.yml`: `hai-session-runner` environment. Change `HAI_FRONTEND_PUBLIC_URL` and `HAI_SIMULATOR_PUBLIC_URL` to `${…:-}` (blank default), and add `HAI_PUBLIC_HOST=${HAI_PUBLIC_HOST:-}`, `HAI_FRONTEND_PUBLIC_PORT=${HAI_FRONTEND_PUBLIC_PORT:-}` and `HAI_SIMULATOR_PUBLIC_PORT=${HAI_SIMULATOR_PUBLIC_PORT:-}`.
- `services/human_ai_interaction_testing/settings.py`: in `from_environment`, replace the two `_read_text` calls for `frontend_public_url` / `simulator_public_url` with a new helper `_read_public_url(environ, url_name, port_name, default_port, local_default)`, plus a port parser `_read_port(environ, name, default)` that raises `ValueError` like `_read_positive_float` does.
- `services/human_ai_interaction_testing/tests/test_settings.py`: **new**, the settings rows of 9.2.
- `README.md`: the "Public access" section (FR-13).

**Existing interfaces to respect:**
- `backend_compose` / `simulator_compose`: keep their signatures and add the public override `-f` only when public access is on. File order: base, then `keycloak.override.yml` / `simulator.override.yml`, then the public override.
- `runtime/interactiveai.env`: add `HAI_FRONTEND_PUBLIC_PORT=` (written always; the override is only applied when public access is on).
- The simulator compose has no env file, so `export HAI_SIMULATOR_PUBLIC_PORT` before calling `simulator_compose`.
- `FRONTEND_URL` keeps feeding `configure_keycloak`, `setup_authentication`, `verify` and `print_summary` unchanged.
- `HAISettings` field names and types unchanged.

**Library constraints:** No new dependencies. Bash only uses tools the script already requires (`docker`, `curl`, python), plus `ss` (optional, FR-09).
**Test framework:** pytest (existing suite in `services/human_ai_interaction_testing/tests/`). Shell behaviour is verified with `bash -n`, compose `config`, and the server acceptance run.
**Style notes:** Research-code style: modular, descriptive names, full docstrings in Python, and a short "why" comment above each non-obvious shell block, matching the existing script.

**Requirement priority order for implementation:**
1. FR-01, FR-05: `.env` / `.env.example` / `.gitignore` and the parser in the script.
2. FR-02, FR-08: on/off decision and port validation (script).
3. FR-03, FR-04: the two override files, wired into `backend_compose` / `simulator_compose`.
4. FR-06: `FRONTEND_URL` derivation.
5. FR-07: `settings.py`, `docker-compose.yml` and `test_settings.py`.
6. FR-12: verify switching off works (no extra code expected).
7. FR-09, FR-10, FR-11: port pre-check, verify step, summary.
8. FR-13: README.

**Known gotchas / non-obvious decisions:**
- Do **not** use `ports: !override` in the public overrides. The internal ports 3200 and 5122 must stay (`host.docker.internal:3200` / `:5122` wiring).
- The container side is fixed: frontend nginx listens on **80**, the simulator on **5000**. Only the host side is configurable. (A server-side edit of a container port, `8089` instead of `8080`, already broke Keycloak once.)
- Compose resolves `.env` from the project directory. For `hai-session-runner` that's `use-cases/dutch-node-hai-testing/`. The InteractiveAI backend and simulator stacks have other project directories, so they do **not** see `.env`; the setup script passes the values explicitly (env file / `export`).
- Parse `.env`; never `source` it (same reasoning as the `.secrets` handling in the script header).
- When Python derives the URLs, a blank `HAI_FRONTEND_PUBLIC_URL` must count as unset. `_read_text` already treats blank as unset; reuse that behaviour.
- The public URL keeps the explicit port (`http://h:443`), matching what Keycloak's Frontend URL is set to.
- `up -d` recreates `frontend` when its port list changes, and the simulator is always `--force-recreate`d, so the step-5 network connections are redone by the same run. Nothing extra is needed.
- On Windows (Git Bash), `ss` doesn't exist. FR-09 must degrade to a warning, not fail.

**What the implementation must NOT do:**
- Modify anything inside the InteractiveAI submodule.
- Publish 443/8443 when `HAI_PUBLIC_HOST` is unset.
- Expose Keycloak, the orchestrator, `hai-session-runner` or any backend service port beyond the two above.
- Add TLS, certificates or an extra reverse-proxy container.
- Change `HAI_SIMULATOR_CAB_SERVER`, `POWERGRID_SIMU_UPSTREAM` or other internal URLs.
- Write to `.env` (it is operator-owned).
