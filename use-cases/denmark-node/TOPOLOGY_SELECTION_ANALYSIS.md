# Denmark Node: Topology Selection Flow Analysis

> **Implementation note:** The confirmation flow below describes the intended
> interaction. The current `/confirm` handler records a decision in memory after
> the three-node workflow finishes; it does not block or resume WP3 execution.
> See [integration status](INTEGRATION_STATUS.md).

## Overview

The Denmark node implements a **hybrid semi-automatic topology selection system**:
- Topologies are **enumerated and ranked automatically** based on optimization criteria
- The **best topology is auto-selected** as a recommendation
- **User confirmation is required** before the topology is accepted for downstream work
- Users can **reject and request regeneration** if unsatisfied

---

## System Architecture

### Three Primary Services

1. **config_assistant** - Parses user prompt → generates run_config
2. **topology_generator** - Enumerates feasible topologies, ranks them, selects best
3. **draw_topology** - Renders SVG visualizations for all candidates
4. **web_ui** - User-facing interface for confirmation/rejection

### Data Flow Pipeline

```
User Prompt (web_ui)
    ↓
config_assistant (LLM parsing)
    ↓ [run_config with supplies/loads]
topology_generator (dh_network_generator.py)
    ↓ [list of feasible topologies]
draw_topology (SVG rendering)
    ↓ [visualizations]
web_ui (recommendation + confirmation)
    ↓ [user accept/reject]
Workflow continues / restarts
```

---

## Topology Generation & Enumeration

### How Topologies Are Generated

**Source:** `dh_network_generator.py`, `enumerate_networks()` function

The generator performs **exhaustive enumeration** of feasible network configurations:

1. **Switchboard Model**
   - Each switchboard has multiple "headers" (independent F/R rail pairs)
   - Terminals (supplies, loads, pipes) are valved onto specific headers
   - Two terminals connect hydraulically only if on the same header/bus

2. **Enumeration Strategy**
   - Iterates through all subsets of available pipes (0 to max)
   - For each subset, checks topological constraints:
     - Graph connectivity between required switchboards
     - No dead-end branches (each pipe must be load-bearing)
     - Optional cycles allowed (configurable)

3. **Pipe Modes**
   - Each pipe can operate in two modes:
     - **Bypass mode**: Direct flow, buses share one hydraulic circuit
     - **Heat Exchanger (HX) mode**: Thermal coupling only, circuits stay separate
   - Both modes are explored for each pipe configuration

4. **Bus & Header Assignment**
   - Terminals at same switchboard are partitioned onto distinct headers
   - Respects terminal-level restrictions (which headers each terminal can reach)
   - Generates all valid header combinations

### Configuration Parameters

```python
enumerate_networks(
    supplies: list,           # Heat source terminals (e.g., "310-D::CHP")
    loads: list,              # Heat sink terminals (e.g., "716-D::Dumpload")
    allow_hx=True,            # Allow heat exchanger mode
    allow_bypass=True,        # Allow bypass (direct flow) mode
    allow_cycles=False,       # Prevent unnecessary loops
    max_results=20,           # Max candidates to return (default)
    max_extra_pipes=1,        # Max additional pipes beyond minimum tree
    load_topology="any",      # "any" | "series" | "parallel"
)
```

---

## Topology Ranking & Selection

### Automatic Ranking Criteria

**Source:** `topology_generator/service.py`, `_generate_topology()` function

The generator automatically ranks all feasible topologies using a **multi-level cost key**:

```python
ranked_networks = sorted(
    topology_data,
    key=lambda network: (
        len(network.get("pipes", {})),           # Primary: fewest pipes
        network.get("pipe_m", float("inf")),     # Secondary: shortest total pipe length
        network.get("signature", ""),            # Tertiary: stable signature tie-break
    ),
)
best_network = ranked_networks[0]  # Automatically select best
```

### Selection Criteria Explained

| Priority | Criterion | Rationale |
|----------|-----------|-----------|
| **1st** | Fewest pipes | Simplest topology, lower cost/complexity |
| **2nd** | Shortest total pipe length | Reduced material, installation, maintenance |
| **3rd** | Signature (stable hash) | Deterministic tie-breaking for reproducibility |

### Signature Definition

```python
signature = SHA1(
    "P1:bypass;P2:hx;...;310-T\|header_a\|term1,term2;...;S:supply1,supply2;L:load1,load2"
)[:8]  # 8-char fingerprint
```

**Properties:**
- Uniquely identifies topology structure (pipes, modes, bus assignments)
- **Stable across runs** - same physical topology = same signature
- **Independent of generation order** - not affected by how results are sorted
- Used for tracking and comparison

### Metrics Provided

For each topology, the system reports:
- `pipe_count` - Number of pipe segments used
- `pipe_m` - Total pipe length (forward + return)
- `trench_m` - Trenching distance required
- `water_m3` - Water volume in circuit
- `load_pattern` - How loads are connected (series/parallel/mixed)
- `header_variants` - How many distinct valid header assignments exist

---

## Selection Flow: Automatic vs. Manual

### Is Selection Automatic or Manual?

**Answer: Semi-automatic with user confirmation**

1. **Automatic Selection**
   ```python
   # In topology_generator/service.py
   best_network = ranked_networks[0]  # Automatically selected

   payload = {
       "recommended_network": best_network.get("signature"),
       "reason": "lowest pipe count and shortest practical path...",
       "user_confirmation_required": True,
       ...
   }
   ```

   The system **automatically selects** the topology with:
   - Minimum pipe count
   - Among those, shortest total pipe length
   - Uses signature for deterministic tie-breaking

2. **User Confirmation Required**
   ```javascript
   // web_ui/app.py - JavaScript frontend
   confirmBtn.onclick = async () => {
       const response = await fetch('/confirm', {
           body: JSON.stringify({
               workflow_id: workflowId,
               selected_signature: selectedSignature,
               accepted: true
           })
       });
   };
   ```

   The web UI displays:
   - Recommended topology with full network details
   - SVG drawing of the recommended topology
   - Selection criteria and comparison to next-best option
   - Configuration assumptions used to generate it

3. **User Decision Options**
   - **Accept** - Topology confirmed, workflow continues downstream
   - **Reject** - User declines, workflow can be restarted with different prompt

### Why Semi-Automatic?

**Rationale:**
- The enumerated candidates are typically small (max 20 by default)
- Automatic ranking by cost is optimal for typical network design goals
- User confirmation provides a **safety gate** for validation
- Users can see the decision rationale and alternatives (next-best option)
- Rejected workflows can be re-run with refined parameters

---

## General Selection Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│ WEB_UI: User enters prompt                                       │
│ "Use CHP at 310-D as supply, feed heat dumpload at 716-D"       │
└────────────────────┬────────────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────────────┐
│ CONFIG_ASSISTANT: Parse prompt with LLM                          │
│ Output: run_config = {                                           │
│   supplies: ["310-D::CHP"],                                      │
│   loads: ["716-D::Dumpload"],                                    │
│   allow_hx: true,                                               │
│   allow_bypass: true,                                           │
│   how_many: 20                                                  │
│ }                                                               │
└────────────────────┬────────────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────────────┐
│ TOPOLOGY_GENERATOR: enumerate_networks()                         │
│ Generate ALL feasible topologies:                                │
│ - Iterate subsets of pipes                                      │
│ - Try bypass vs HX modes for each                               │
│ - Partition terminals onto headers                              │
│ - Validate constraints                                          │
│                                                                 │
│ Result: 20 feasible topologies (capped by max_results)          │
└────────────────────┬────────────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────────────┐
│ RANKING & AUTO-SELECTION:                                        │
│ Sort by (pipes_count, pipe_m, signature)                        │
│ ┌────────────────────────────────────────────────────────────┐  │
│ │ TOPOLOGY 1 ← SELECTED (best score)                        │  │
│ │   Pipes: 4, Length: 250m, Signature: 3f7a2c1b            │  │
│ ├────────────────────────────────────────────────────────────┤  │
│ │ TOPOLOGY 2 ← Next best                                     │  │
│ │   Pipes: 5, Length: 300m, Signature: 5e1b9d4a            │  │
│ ├────────────────────────────────────────────────────────────┤  │
│ │ TOPOLOGY 3 ... TOPOLOGY 20                                 │  │
│ └────────────────────────────────────────────────────────────┘  │
└────────────────────┬────────────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────────────┐
│ DRAW_TOPOLOGY: Render SVG for all topologies                     │
│ - Generate visual representations                               │
│ - Store drawings keyed by topology signature                    │
└────────────────────┬────────────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────────────────────┐
│ WEB_UI: Display Recommendation                                   │
│ ┌────────────────────────────────────────────────────────────┐  │
│ │ RECOMMENDED TOPOLOGY                                       │  │
│ │ ├─ Signature: 3f7a2c1b                                    │  │
│ │ ├─ Reason: "lowest pipe count..."                         │  │
│ │ ├─ Details: 4 pipes, 250m trench, 500m total pipe        │  │
│ │ ├─ Comparison: Next option is 5 pipes, 300m              │  │
│ │ ├─ Network diagram: [SVG DRAWING]                         │  │
│ │ ├─ Configuration: {...run_config...}                      │  │
│ │ └─ [CONFIRM] [REJECT]                                     │  │
│ └────────────────────────────────────────────────────────────┘  │
└────────────┬──────────────────────────────┬──────────────────────┘
             ↓                              ↓
       ┌──────────────┐             ┌──────────────┐
       │   CONFIRM    │             │    REJECT    │
       │ Topology     │             │ Restart with │
       │ accepted,    │             │ new prompt   │
       │ workflow     │             │              │
       │ continues    │             │              │
       └──────────────┘             └──────────────┘
```

---

## Key Implementation Details

```python
# This is not a core feature for now
```
### Topology Signature System

The signature ensures **idempotency and traceability**:

```python
# From dh_network_generator.py, Solution.signature property

parts = [
    f"{p}:{self.pipe_modes[p]}"               # Pipe name and mode
    for p in sorted(self.pipes_used)
]
parts += [
    f"{b.switchboard}|{b.header}|{','.join(b.terminals)}"
    for b in sorted(self.buses, key=lambda x: (x.switchboard, x.header))
]
parts.append(f"S:{','.join(sorted(self.supply_ids))}")
parts.append(f"L:{','.join(sorted(self.load_ids))}")

blob = ";".join(parts).encode("utf-8")
signature = hashlib.sha1(blob).hexdigest()[:8]
```

**Usage in web_ui:**
```javascript
// When user confirms, the signature is tracked
let selectedSignature = network?.signature;  // e.g., "3f7a2c1b"

confirmBtn.onclick = async () => {
    await fetch('/confirm', {
        body: JSON.stringify({
            workflow_id,
            selected_signature: selectedSignature,  // Tracked
            accepted: true
        })
    });
};
```

### Ranking in service.py

```python
# Automatic ranking happens during topology generation
ranked_networks = sorted(
    topology_data,
    key=lambda network: (
        len(network.get("pipes", {})),
        network.get("pipe_m", float("inf")),
        network.get("signature", ""),
    ),
)

# Best is selected, but ALL are provided to draw_topology
payload = {
    "topology_count": len(solutions),
    "networks": topology_data,           # ALL candidates
    "recommended_network": best_network, # Best candidate
    "report": {
        "recommended_network": best_network.get("signature"),
        "reason": "lowest pipe count and shortest practical path...",
        "selection": {
            "candidate_count": len(ranked_networks),
            "recommended": network_metrics(best_network),
            "next_best": network_metrics(ranked_networks[1]),
            "criteria": ["fewest pipes", "shortest total pipe length", "..."],
        },
    }
}
```

---


### Why User Confirmation?

1. **Safety gate** - Prevents automated decisions from propagating unchecked
2. **Domain validation** - User can catch unexpected results from prompt ambiguity
3. **Workflow control** - Users can reject and try different prompts
4. **Auditability** - Clear record of human acceptance

### Scalability Considerations

- **max_results=20** - Caps candidates to manageable number
- **max_extra_pipes=1** - Limits exponential search space
- **allow_cycles=False** - Prevents redundant alternatives
- **load_topology filtering** - Can restrict to series/parallel only
- All 20 candidates are **still rendered** even though only 1 is recommended

---

## Topology Validation

**Note:** The system also includes a `ValidateTopology` operation (dummy implementation):

```python
def execute_ValidateTopology(request: ExecuteRequest) -> ExecuteResponse:
    """Fast path: report a placeholder validation verdict."""
    design = resolve_input(request.inputs, default={}) or {}
    output = {
        "experiment_id": design.get("experiment_id"),
        "valid": True,
        "violations": [],
        "generated_by": "topology_generator (dummy)",
    }
```

---

## Related Files

- **topology_generator/dh_network_generator.py** - Core enumeration algorithm
- **topology_generator/service.py** - Ranking and auto-selection logic
- **web_ui/app.py** - User confirmation UI (JavaScript frontend)
- **draw_topology/service.py** - SVG rendering pipeline
- **syslab_heat_topology.yaml** - Infrastructure definition (switchboards, terminals, pipes)
