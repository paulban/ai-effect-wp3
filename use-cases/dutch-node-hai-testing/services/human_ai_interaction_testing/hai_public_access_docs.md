# HAI Public Access — Documentation

*Generated from the source docstrings and comments. Update the source, then regenerate; don't edit by hand.*

**Spec:** `.claude/specs/hai-public-access-spec.md`
**Generated:** 2026-11-25

---

## Overview

Opt-in public access to the HAI stack: when `HAI_PUBLIC_HOST` is set in the
use case's server-only `.env`, InteractiveAI is served on
`http://<host>:443` and the PowerGrid simulator on `http://<host>:8443`
(plain HTTP, TU Delft network only). The ports are published **in addition**
to the internal 3200 / 5122, Keycloak's Frontend URL becomes the public
address, and `hai-session-runner` hands participants the public links.
Without `HAI_PUBLIC_HOST`, nothing changes.

---

## Configuration (`use-cases/dutch-node-hai-testing/.env`, see `.env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `HAI_PUBLIC_HOST` | unset (off) | Hostname participants type |
| `HAI_FRONTEND_PUBLIC_PORT` | `443` | Server port for InteractiveAI (container side fixed at nginx's 80) |
| `HAI_SIMULATOR_PUBLIC_PORT` | `8443` | Server port for the simulator (container side fixed at 5000) |

Read by `setup_interactiveai.sh` (parsed, never sourced; the shell wins) and
by Compose for `hai-session-runner` (`.env` in the project directory).

---

## Modules

### `scripts/setup_interactiveai.sh`

#### Constants

- `PUBLIC_FRONTEND_OVERRIDE`, `PUBLIC_SIMULATOR_OVERRIDE`: the two override files, applied only when public access is on.
- `DOTENV_FILE`: the use case's `.env`.
- `FRONTEND_LOCAL_PORT=3200`, `SIMULATOR_LOCAL_PORT=5122`: host ports of the base compose files. They stay published because the simulator and the frontend reach each other through them.
- `DEFAULT_FRONTEND_PUBLIC_PORT=443`, `DEFAULT_SIMULATOR_PUBLIC_PORT=8443`: the ports TU Delft ICT opened.

#### Functions

##### `trim <text>`
Strips leading and trailing whitespace.

##### `read_dotenv_value <KEY>`
Prints the value of one `KEY=value` line of `.env`, or nothing. Parsed rather
than sourced, so the file cannot run code or set other variables. Skips
comments and blank lines, strips whitespace and one pair of surrounding
quotes, drops an inline ` # comment` from unquoted values, and, as Compose
does, lets the last occurrence of a key win. (FR-05)

##### `read_setting <NAME> <default>`
Prints a setting: the shell's value when the variable is set there (even
blank, so `HAI_PUBLIC_HOST= scripts/setup_interactiveai.sh` switches public
access off), else the `.env` value, else the default. (FR-05)

##### `validate_port <NAME> <value>`
Exits unless the value is a port number between 1 and 65535. (FR-08)

##### `backend_compose <args…>` / `simulator_compose <args…>`
Unchanged signatures. They append `public-frontend.override.yml` /
`public-simulator.override.yml` after the existing override when public
access is on. (FR-03, FR-04)

##### `resolve_public_access`
Decides whether public access is on and derives its URLs. It sets
`PUBLIC_HOST`, `FRONTEND_PUBLIC_PORT`, `SIMULATOR_PUBLIC_PORT`,
`PUBLIC_SIMULATOR_URL` and `FRONTEND_URL`. When on, it validates both ports,
requires them to differ from each other and from 3200, 5122 and `KC_PORT`, and
exports both for the overrides. `FRONTEND_URL` precedence: explicit
`HAI_FRONTEND_PUBLIC_URL` > `http://<host>:<frontend port>` >
`http://localhost:3200`. (FR-02, FR-06, FR-08)

##### `check_public_port <port> <owner container>` / `check_public_ports`
Exits if a public port is held by something other than the stack's own
container from an earlier run. Needs `ss`; without it (e.g. Git Bash on
Windows), it warns and skips. Runs after any `--clean` / `--wipe` teardown and
before the backend starts. (FR-09)

##### `verify` (extended)
When public access is on, also checks that the simulator answers on
`PUBLIC_SIMULATOR_URL`; a failure is a warning. The public frontend URL is
already exercised by step 2's login. (FR-10)

##### `print_summary` (extended)
When public access is on, prints both public URLs, notes "plain HTTP, TU
Delft network or VPN only", and warns that login now works only at the
public URL. (FR-11)

### `public-frontend.override.yml` / `public-simulator.override.yml`

They add `0.0.0.0:${HAI_FRONTEND_PUBLIC_PORT:-443}:80` to `frontend` and
`0.0.0.0:${HAI_SIMULATOR_PUBLIC_PORT:-8443}:5000` to the simulator's `app`.
They use no `!override`, so the ports are appended to 3200 / 5122.
(FR-03, FR-04)

### `docker-compose.yml` (`hai-session-runner`)

Passes `HAI_PUBLIC_HOST`, `HAI_FRONTEND_PUBLIC_PORT` and
`HAI_SIMULATOR_PUBLIC_PORT`. `HAI_FRONTEND_PUBLIC_URL` /
`HAI_SIMULATOR_PUBLIC_URL` now default to blank, so `settings.py` derives
them. (FR-07)

### `services/human_ai_interaction_testing/settings.py`

#### Constants

- `LOCAL_FRONTEND_PUBLIC_URL = "http://localhost:3200"`, `LOCAL_SIMULATOR_PUBLIC_URL = "http://localhost:5122"`: links without public access.
- `DEFAULT_FRONTEND_PUBLIC_PORT = 443`, `DEFAULT_SIMULATOR_PUBLIC_PORT = 8443`: used when the host is set and no port is given.
- `MINIMUM_PORT = 1`, `MAXIMUM_PORT = 65535`: valid TCP port range.

#### Functions

##### `_read_port(environ, name, default) -> int`
Returns a TCP port number from the environment.
**Raises** `ValueError` if the value is set but is not an ASCII integer
between `MINIMUM_PORT` and `MAXIMUM_PORT`; the message names the variable.
(FR-08)

##### `_read_public_url(environ, url_name, port_name, default_port, local_default) -> str`
Returns the address a participant's browser uses for one UI. Precedence: the
explicit URL variable; else `http://<HAI_PUBLIC_HOST>:<port>` when a public
host is set; else the localhost address. The setup script applies the same
rule to Keycloak's Frontend URL, so the link given to the participant is the
address login works at.
**Raises** `ValueError` if a public host is set and the port variable is invalid.

```python
>>> _read_public_url({"HAI_PUBLIC_HOST": "h"}, "HAI_FRONTEND_PUBLIC_URL",
...                  "HAI_FRONTEND_PUBLIC_PORT", 443, "http://localhost:3200")
'http://h:443'
```

##### `HAISettings.from_environment` (changed)
`frontend_public_url` and `simulator_public_url` now come from
`_read_public_url`. It also raises `ValueError` for an invalid public port
while `HAI_PUBLIC_HOST` is set.

---

## Requirements Coverage

| Requirement | Status | Implemented in |
|-------------|--------|----------------|
| FR-01 | ✓ | `.env.example`, `.gitignore` |
| FR-02 | ✓ | `setup_interactiveai.sh: resolve_public_access()`; `settings.py: _read_public_url()` |
| FR-03 | ✓ | `public-frontend.override.yml`; `backend_compose()` |
| FR-04 | ✓ | `public-simulator.override.yml`; `simulator_compose()` |
| FR-05 | ✓ | `read_dotenv_value()`, `read_setting()` |
| FR-06 | ✓ | `resolve_public_access()` (`FRONTEND_URL`) |
| FR-07 | ✓ | `settings.py: _read_public_url()`; `docker-compose.yml` |
| FR-08 | ✓ | `validate_port()`, `resolve_public_access()`; `settings.py: _read_port()` |
| FR-09 | ✓ | `check_public_port()`, `check_public_ports()` |
| FR-10 | ✓ | `verify()` |
| FR-11 | ✓ | `print_summary()` |
| FR-12 | ✓ | No extra code: `up -d` recreates `frontend` on a port change, and the simulator is always recreated |
| FR-13 | ✓ | `README.md` "Public access" |

### Deviations

- **FR-05 / FR-06:** besides the three keys, the script also reads
  `HAI_FRONTEND_PUBLIC_URL` through `read_setting`, so from `.env` as well as
  the shell. Compose passes that variable from `.env` to `hai-session-runner`,
  and reading it in both places keeps Keycloak's Frontend URL and the
  participant link the same.

---

## Verification Status

| Check | Result |
|---|---|
| `tests/test_settings.py` (FR-07, FR-08, AC-5, AC-7 Python side) | 11 passed |
| Full service suite | 69 passed, 5 skipped (grid2op not installed locally) |
| Script harness: `.env` parsing, AC-7 (script side), AC-8, off/on URLs, no code execution from `.env` | 17 / 17 passed |
| `docker compose config` merges (spec 9.2 compose rows) | `frontend` 3200 + `0.0.0.0:443`; simulator 5122 + `0.0.0.0:8443`; no 443 when off |
| AC-1 to AC-4, AC-6 | **Pending**: need the server and a laptop on the TU Delft network |

## Open Items

- AC-1 to AC-4 and AC-6 must be run on `aieffect-dutch01`.
- Spec Open Question 1 (Keycloak redirect URIs) gets settled during AC-2.
