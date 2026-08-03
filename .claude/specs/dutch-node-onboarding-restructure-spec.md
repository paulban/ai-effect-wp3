# Dutch Node Onboarding-Guide Conformance Spec

**Status:** Draft
**Author:** Paul Bannmüller
**Date:** 2026-08-03
**Domain:** Software Feature — WP3 TEF Use-Case Packaging
**Tier:** B (focused spec)

---

## 1. Overview

`use-cases/ONBOARDING-GUIDE.md` defines how a use case is prepared for the AI-Effect portal: a `services/` directory of proto files plus a `connections.json`, fed to `scripts/onboarding-export-generator.py`, which emits `blueprint.json`, `dockerinfo.json`, `generation_metadata.json` and `microservice/*.proto`.

`use-cases/dutch-node/` predates that guide. Its export was produced by a bespoke script (`test_pipelines/build_ai4eu_package.py`) with a hand-written blueprint and dockerinfo, covering only two of the three TEF services. The standard generator could not run against it at all — no `services/` directory, no `connections.json`.

This spec covers restructuring the Dutch node onto the standard tooling. The complicating factor is that the three TEF services are **not a data pipeline**: the data synthesizer produces grid data for varying inputs and does not feed the benchmark; the benchmark uses a published synthetic *training* dataset while its *testing* dataset is never published; the human-AI interaction testing service is unconnected and runs a fixed grid + time-series case. The onboarding format assumes RPC-to-RPC wiring, so representing independent services required a deliberate decision plus a small generator extension.

---

## 2. Goals

1. All three TEF services are packaged by `scripts/onboarding-export-generator.py`, with no bespoke packaging script remaining.
2. Each package produces exactly **one** orchestrator start node, so a workflow submission invokes exactly one service operation.
3. Blueprints reference pullable image names under an AI-EFFECT registry namespace.
4. The generator change is backward compatible: the five pre-existing use cases generate byte-identical output.
5. The human-AI interaction testing service is represented in a portal package for the first time.

## 3. Non-Goals

- Publishing images to `ghcr.io/ai-effect` (the `image` values are recorded; pushing is a separate, credentialed step).
- Uploading any package to the portal.
- Representing the benchmark's published training dataset (HuggingFace or otherwise) in the proto or export metadata.
- Changing any service's business logic, gRPC interface, or proto message definitions.
- Re-establishing a data-flow connection between the synthesizer and the benchmark.
- Fixing the pre-existing drift between other use cases' committed `export/` directories and their source protos (see FR-07 note).

---

## 5.1 Functional Requirements

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-01 | `use-cases/dutch-node/` is split into `dutch-node-data-synthesizer`, `dutch-node-benchmarking`, `dutch-node-hai-testing`, each with `services/<snake_case>/proto/<name>.proto` | Must | One portal Solution each |
| FR-02 | Each use case has a `connections.json` with a one-entry `service_mapping` and `"connections": []` | Must | No inter-service wiring exists |
| FR-03 | The generator accepts an optional `operations` allowlist per service_mapping entry and exports only those RPCs | Must | Without it, every RPC becomes a start node |
| FR-04 | The generator accepts an optional `image` per service_mapping entry, overriding the derived `<use-case>-<service>:latest` | Must | Derived names carry no registry namespace |
| FR-05 | Entry operations are `ConfigureAndSynthesize`, `RunBenchmark`, `StartHumanAISession` | Must | All `Get*` RPCs are vendor-called, not scheduled |
| FR-06 | Each generated blueprint yields exactly one start node when parsed by the orchestrator's `BlueprintParser` | Must | Direct verification of goal 2 |
| FR-07 | The five pre-existing use cases generate output identical to pre-change, ignoring `pipeline_id` and `creation_date` | Must | Those two fields are regenerated every run. Note: their **committed** `export/` dirs are already stale vs their source protos — a pre-existing upstream condition, out of scope |
| FR-08 | `build_ai4eu_package.py`, the old `export/` and `dutch-node.zip` are removed | Must | Superseded by the standard generator |
| FR-09 | Redundant per-service `blueprint.json` / `dockerinfo.json` are removed; `run_workflow.sh` reads the generated `export/` instead | Must | Single source of truth |
| FR-10 | Submodule paths in `.gitmodules` follow the HAI service, with pinned SHAs unchanged | Must | `2a41af9` InteractiveAI, `fa52f51` hmisurveys |
| FR-11 | ~~The benchmark service still compiles stubs for the data synthesizer's proto after the split~~ | Superseded | Withdrawn 2026-08-03: the benchmark draws from a preset grid2op scenario, so the synthesizer dependency and `external_proto/` were removed entirely |
| FR-12 | ~~`test_synth_to_benchmark.py` is retained as an explicitly manual cross-service check~~ | Superseded | Withdrawn 2026-08-03 with FR-11; there is no cross-service path left to check |
| FR-13 | The benchmark's scenario is preset configuration (`l2rpn_case14_sandbox`), never supplied by the data synthesizer | Must | Added 2026-08-03; see revision 0.2 |

---

## 8. Open Questions

| # | Question | Owner | Due |
|---|----------|-------|-----|
| 1 | Does anyone hold `ghcr.io/ai-effect` package write access? Values are recorded either way; nothing is pushed by this work. | Paul | Before portal upload |
| 2 | Should the benchmark's published training dataset (HuggingFace link?) be surfaced in the proto or export metadata? | Paul | Follow-up |
| 3 | ~~`benchmarking/run_workflow.sh` still health-checks the synthesizer and aborts if it is down.~~ **Resolved 2026-08-03:** vestigial. The workflow only ever sent an inline payload with a preset `env_name`, so the gate could abort a run for a service it never called. Gate removed and the gRPC ingestion path deleted. | Paul | Closed |

---

## 9. Verification

### 9.1 Acceptance Criteria

**FR-03 / FR-05 — operations allowlist:**
> Given `data_synthesizer` declares `"operations": ["ConfigureAndSynthesize"]` and its proto defines four RPCs, when the generator runs, then `export/blueprint.json` contains exactly one operation signature.

**FR-04 — image override:**
> Given a service_mapping entry declares `"image": "ghcr.io/ai-effect/dutch-node-benchmarking:latest"`, when the generator runs, then that exact string appears as the node's `image` in `blueprint.json` and as `image_name` in `generation_metadata.json`.

**FR-06 — single start node:**
> Given a generated `blueprint.json`, when parsed by `orchestrator/src/services/blueprint_parser.py`, then `len(graph.start_nodes) == 1`.

**FR-07 — no regression:**
> Given the five pre-existing use cases, when regenerated with the modified generator, then output matches the pre-change baseline once `pipeline_id` and `creation_date` are stripped.

**FR-10 — submodules intact:**
> Given the HAI service has moved, when `git submodule status` is run, then both submodules report their original SHAs, and `git clone --recurse-submodules` still populates both directories.

### 9.2 Test Scenarios

| Scenario | Input / State | Expected Result | Covers |
|----------|---------------|-----------------|--------|
| Allowlist filters | 4-RPC synth proto, allowlist of 1 | 1 operation in blueprint | FR-03 |
| Allowlist absent | Any pre-existing use case | Connection-derived filtering unchanged | FR-07 |
| Unknown operation | Allowlist names an RPC not in the proto | Warning printed, no crash | FR-03 |
| Image override absent | Pre-existing use case | Derived `<use-case>-<service>:latest` | FR-07 |
| Start node count | Each of the three new blueprints | Exactly 1 | FR-06 |
| Generator regression | All 5 pre-existing use cases | Identical modulo volatile fields | FR-07 |
| Service suites | HAI and benchmarking test suites | HAI 51 pass; benchmarking 8 pass / 1 pre-existing failure | FR-01 |

### 9.3 Definition of Done

- [ ] All Must requirements implemented
- [ ] Acceptance criteria passing
- [ ] Five-use-case regeneration shows no regression
- [ ] Three `export/` dirs each contain all four artefacts including `generation_metadata.json`
- [ ] `git submodule status` unchanged; recursive clone populates both submodules
- [ ] Benchmark image builds with the vendored proto
- [ ] `use-cases/ONBOARDING-GUIDE.md` documents `operations` and `image`
- [ ] Generator change proposed upstream to `AI-EFFECT/ai-effect-wp3`

---

## 12. Implementation Handoff

> Single source of truth for the implementation agent.

**Language & runtime:** Python 3.11+ (generator, services); Bash (`run_workflow.sh`)
**Execution model:** Sync. The generator is a single-pass CLI script with no I/O concurrency.
**Entry point:** `scripts/onboarding-export-generator.py`
**Code location:** Extend the existing generator in place; new use-case directories under `use-cases/`
**Existing interfaces to respect:** `OnboardingExportGenerator.scan_services`, `.generate_blueprint_node`, `.generate_metadata`. The `connections.json` schema must stay backward compatible — `operations` and `image` are both optional.
**Library constraints:** Standard library only in the generator (currently `json`, `shutil`, `argparse`, `zipfile`, `pathlib`, `datetime`, `uuid`). Do not add dependencies.
**Test framework:** None for the generator — it has no test suite. The five-use-case regeneration diff is the regression guard; use a normalising comparison that strips `pipeline_id` and `creation_date`.
**Style notes:** Research-code style — descriptive names, docstrings explaining *why*, comments only where intent is non-obvious.

**Requirement priority order for implementation:**
1. FR-07 — capture the pre-change baseline *before* editing the generator
2. FR-03, FR-04 — generator extension
3. FR-01, FR-02 — directory split and connections.json
4. FR-09, FR-11 — repair references broken by the split
5. FR-10 — submodule paths
6. FR-06, FR-08, FR-12 — regenerate, verify, retire

**Known gotchas / non-obvious decisions:**
- The orchestrator schedules per **operation**, not per service: `GraphNode.key = f"{container_name}:{operation_name}"` (`orchestrator/src/models/graph.py:20`), one graph node per `operation_signature_list` entry. Every operation without dependencies is a start node. This is why the allowlist and the three-way split are both required.
- RPC filtering is `if connected_methods and ...` — an empty set means *no* filtering, so a connectionless service exports every RPC. The allowlist must take precedence over the connection-derived path, not merge with it.
- `service['name']` is the `ip_address` from service_mapping, not the directory name. The derived image name is `<use-case-dir>-<ip_address>:latest` and has no registry host, so it cannot be pulled.
- `benchmarking/Dockerfile` pre-compiles protos at **build** time, so runtime path resolution never fires in the container. Both host and build paths must be fixed; testing only one hides the other.
- ~~The benchmark depends on the synthesizer's proto, vendored to `services/benchmarking/external_proto/`.~~ **Obsolete as of 0.2:** the dependency was removed with FR-11. The underlying constraint still holds for any future case — `services/<name>/proto/` must hold exactly one file, or the generator's choice of service interface becomes ambiguous.
- VS Code's git extension holds handles on submodule directories listed in `git.scanRepositories`, which makes `git mv` of their parent fail with "Permission denied" on Windows. Close the editor before moving.

**What the implementation must NOT do:**
- Do not add connections between the three services; they are independent by design.
- Do not put a second `.proto` in any `services/<name>/proto/` directory.
- Do not change proto message definitions or service business logic.
- Do not regenerate or "fix" the other use cases' committed `export/` directories — their drift is pre-existing and out of scope.
- Do not push images or submodule commits as part of this work.

---

## 11. Revision History

| Version | Date | Author | Summary of changes |
|---------|------|--------|--------------------|
| 0.1 | 2026-08-03 | Paul Bannmüller | Initial draft — three-way split, operations allowlist, image override |
| 0.2 | 2026-08-03 | Paul Bannmüller | Benchmark scenario is preset, not synthesizer-fed: withdrew FR-11/FR-12, added FR-13, closed open question 3 |
