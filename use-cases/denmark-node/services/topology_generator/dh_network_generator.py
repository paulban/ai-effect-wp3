#!/usr/bin/env python3
"""
dh_network_generator.py
=======================

Enumerate the feasible district-heating network topologies on the SYSLAB heat
switchboards, given a set of supply components and a set of loads.

Model
-----
The unit of the model is the **bus**: one header at one switchboard, carrying a
forward (F) and a return (R) rail. A switchboard with three headers offers three
buses, and can host three independent hydraulic groups at the same time.

Every component and every pipe end is valved onto exactly one bus at its
switchboard. Two terminals in the same switchboard are hydraulically connected
only if they sit on the same bus.

Each pipe run between two switchboards either

    * bypasses its heat exchanger -> the two buses it joins share one hydraulic
      circuit (same water, direct flow), or
    * passes through the heat exchanger -> the two buses stay hydraulically
      separate and are only thermally coupled.

A *solution* is therefore

    (set of pipes used, HX/bypass per pipe, which terminals share which bus)

subject to:

    1. every requested load is hydraulically or thermally reachable from at
       least one requested supply,
    2. no heat exchanger is short-circuited (both faces on the same circuit),
    3. no dead circuit: every hydraulic circuit is heated, and every leaf
       circuit contains a supply or a load,
    4. the buses used at a switchboard can be given distinct headers that all
       their terminals are allowed to reach.

Because 310-T is one switchboard with three headers, a route may leave on one
header and come back on another - e.g.

    (310-T, header a) --K1-- (330-D, header 1) --K2-- (310-T, header b)

which is a plain chain, not a parallel pair. That is the normal way of using
two runs in the same corridor, and needs no `allow_cycles`.

Usage
-----
    python dh_network_generator.py --supply "310-D::CHP" --load "716-D::Dumpload"
    python dh_network_generator.py --list
    python dh_network_generator.py --supply CHP --load Workshop --valves --max 3
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("pyyaml is required:  pip install pyyaml")


DEFAULT_TOPOLOGY = Path(__file__).with_name("syslab_heat_topology.yaml")

SOURCE_ROLES = {"source", "both"}
SINK_ROLES = {"sink", "both"}


# --------------------------------------------------------------------------
# Infrastructure
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Header:
    name: str
    rails: Tuple[str, ...]
    role: str

    @property
    def is_circuit(self) -> bool:
        return self.role == "circuit" and "F" in self.rails and "R" in self.rails


@dataclass(frozen=True)
class Terminal:
    id: str            # "SWITCHBOARD::Name"
    name: str
    switchboard: str
    role: str          # source | sink | both | passive | pipe
    pipe: Optional[str] = None
    headers: Optional[Tuple[str, ...]] = None   # None = may reach every header

    def __str__(self) -> str:
        return self.id


@dataclass(frozen=True)
class Pipe:
    name: str
    ends: Tuple[str, str]
    terminal: str
    run: str
    has_hx: bool
    hx_at: Optional[str]
    length_m: Optional[float] = None
    dn_mm: Optional[float] = None
    water_m3: Optional[float] = None

    def other_end(self, sb: str) -> str:
        return self.ends[1] if sb == self.ends[0] else self.ends[0]

    def modes(self, allow_hx: bool, allow_bypass: bool) -> Tuple[str, ...]:
        m: List[str] = []
        if allow_bypass:
            m.append("bypass")
        if allow_hx and self.has_hx:
            m.append("hx")
        return tuple(m)


@dataclass
class Switchboard:
    name: str
    building: str
    headers: Dict[str, Header]
    terminals: Dict[str, Terminal] = field(default_factory=dict)

    @property
    def circuit_headers(self) -> Tuple[str, ...]:
        return tuple(h for h, o in self.headers.items() if o.is_circuit)


class Infrastructure:
    def __init__(self, data: dict):
        self.meta = data.get("meta", {})
        self.switchboards: Dict[str, Switchboard] = {}
        self.pipes: Dict[str, Pipe] = {}
        self.terminals: Dict[str, Terminal] = {}

        for sb_name, sb in data["switchboards"].items():
            headers = {
                h: Header(h, tuple(cfg.get("rails", ["F", "R"])), cfg.get("role", "circuit"))
                for h, cfg in sb["headers"].items()
            }
            board = Switchboard(sb_name, str(sb.get("building", "")), headers)
            for t in sb.get("terminals", []):
                tid = f"{sb_name}::{t['name']}"
                hdrs = t.get("headers")
                term = Terminal(tid, t["name"], sb_name, t.get("role", "passive"),
                                headers=tuple(hdrs) if hdrs else None)
                board.terminals[tid] = term
                self.terminals[tid] = term
            self.switchboards[sb_name] = board

        for pname, p in data["pipes"].items():
            pipe = Pipe(
                name=pname,
                ends=(p["ends"][0], p["ends"][1]),
                terminal=p.get("terminal", f"Pipe {pname}"),
                run=p.get("run", pname),
                has_hx=bool(p.get("has_hx", False)),
                hx_at=p.get("hx_at"),
                length_m=p.get("length_m"),
                dn_mm=p.get("dn_mm"),
                water_m3=p.get("water_m3"),
            )
            self.pipes[pname] = pipe
            hdr_map = p.get("headers") or {}
            for sb_name in pipe.ends:
                tid = f"{sb_name}::{pipe.terminal}"
                hdrs = hdr_map.get(sb_name)
                term = Terminal(tid, pipe.terminal, sb_name, "pipe", pipe=pname,
                                headers=tuple(hdrs) if hdrs else None)
                self.switchboards[sb_name].terminals[tid] = term
                self.terminals[tid] = term

    # -- lookup ------------------------------------------------------------
    def resolve(self, name: str) -> str:
        if name in self.terminals:
            return name
        hits = [t for t in self.terminals.values() if t.name.lower() == name.lower()]
        if len(hits) == 1:
            return hits[0].id
        if not hits:
            raise KeyError(f"unknown terminal {name!r}")
        raise KeyError(
            f"ambiguous terminal {name!r}; use one of: "
            + ", ".join(sorted(h.id for h in hits))
        )

    def pipe_terminal_id(self, pipe: Pipe, sb: str) -> str:
        return f"{sb}::{pipe.terminal}"

    def allowed_headers(self, tid: str) -> Tuple[str, ...]:
        term = self.terminals[tid]
        board = self.switchboards[term.switchboard]
        if term.headers is None:
            return board.circuit_headers
        return tuple(h for h in board.circuit_headers if h in term.headers)

    @classmethod
    def load(cls, path: Path = DEFAULT_TOPOLOGY) -> "Infrastructure":
        with open(path, "r", encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))


# --------------------------------------------------------------------------
# Solution pieces
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Bus:
    """One header at one switchboard: an independent F/R rail pair."""
    switchboard: str
    header: str
    terminals: Tuple[str, ...]

    @property
    def id(self) -> str:
        return f"{self.switchboard}/{self.header}"


@dataclass
class Circuit:
    """One hydraulically continuous loop: a set of buses sharing water."""
    index: int
    buses: Tuple[int, ...]                 # indices into Solution.buses
    supplies: Tuple[str, ...] = ()
    loads: Tuple[str, ...] = ()
    pipes_internal: Tuple[str, ...] = ()
    pipes_hx: Tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return chr(ord("A") + self.index)


@dataclass
class Solution:
    infra: Infrastructure
    pipes_used: Tuple[str, ...]
    pipe_modes: Dict[str, str]
    buses: List[Bus]
    circuits: List[Circuit]
    bus_of_terminal: Dict[str, int]
    circuit_of_bus: Dict[int, int]
    hx_links: Tuple[Tuple[int, int, str], ...]
    supply_ids: Tuple[str, ...]
    load_ids: Tuple[str, ...]
    n_header_variants: int = 1
    load_pattern: str = "single"      # series | parallel | mixed | single
    load_order: Tuple[str, ...] = ()  # loads in flow order, when series

    # -- metrics -----------------------------------------------------------
    @property
    def n_hx(self) -> int:
        return sum(1 for m in self.pipe_modes.values() if m == "hx")

    @property
    def n_buses(self) -> int:
        return len(self.buses)

    @property
    def cost_key(self) -> Tuple[int, int, int, int]:
        return (len(self.pipes_used), self.n_hx, len(self.circuits), self.n_buses)

    @property
    def trench_m(self) -> Optional[float]:
        vals = [self.infra.pipes[p].length_m for p in self.pipes_used]
        return None if any(v is None for v in vals) else float(sum(vals))

    @property
    def pipe_m(self) -> Optional[float]:
        t = self.trench_m
        return None if t is None else 2.0 * t

    @property
    def water_m3(self) -> Optional[float]:
        vals = [self.infra.pipes[p].water_m3 for p in self.pipes_used]
        return None if any(v is None for v in vals) else float(sum(vals))

    @property
    def unmeasured_pipes(self) -> Tuple[str, ...]:
        return tuple(p for p in self.pipes_used if self.infra.pipes[p].length_m is None)

    @property
    def length_key(self) -> Tuple[float, int, int]:
        t = self.trench_m
        return (float("inf") if t is None else t, self.n_hx, len(self.circuits))

    @property
    def pattern_note(self) -> str:
        if self.load_pattern == "series" and self.load_order:
            chain = " -> ".join(self.infra.terminals[t].name for t in self.load_order)
            return f"loads in series: {chain}"
        if self.load_pattern == "parallel":
            return "loads in parallel (each fed direct from the supply branch)"
        if self.load_pattern == "mixed":
            return "loads mixed: some in series, some branching"
        return ""

    @property
    def signature(self) -> str:
        """Stable 8-char fingerprint of the topology itself.

        Depends only on what the network IS - the pipes, their bypass/HX mode,
        and which terminals share which bus - never on where it happened to
        land in a result list. The same topology therefore keeps the same
        signature across runs, even when the config or the sort order changes.
        """
        parts = [f"{p}:{self.pipe_modes[p]}" for p in sorted(self.pipes_used)]
        parts += [f"{b.switchboard}|{b.header}|{','.join(b.terminals)}"
                  for b in sorted(self.buses, key=lambda x: (x.switchboard, x.header))]
        parts.append("S:" + ",".join(sorted(self.supply_ids)))
        parts.append("L:" + ",".join(sorted(self.load_ids)))
        blob = ";".join(parts).encode("utf-8")
        return hashlib.sha1(blob).hexdigest()[:8]

    @property
    def multi_bus_switchboards(self) -> Tuple[str, ...]:
        seen: Dict[str, int] = {}
        for b in self.buses:
            seen[b.switchboard] = seen.get(b.switchboard, 0) + 1
        return tuple(sorted(sb for sb, n in seen.items() if n > 1))

    # -- rendering ---------------------------------------------------------
    def describe(self, valves: bool = False) -> str:
        lines: List[str] = []
        pipe_str = ", ".join(f"{p}[{self.pipe_modes[p]}]" for p in self.pipes_used) or "none"
        lines.append(f"id {self.signature}")
        lines.append(
            f"pipes: {pipe_str}   |   buses: {self.n_buses}"
            f"   |   hydraulic circuits: {len(self.circuits)}"
            + (f"   |   header variants: {self.n_header_variants}"
               if self.n_header_variants > 1 else "")
        )
        if self.trench_m is not None:
            lines.append(
                f"  route length: {self.trench_m:.0f} m "
                f"({self.pipe_m:.0f} m of pipe, forward + return)"
                + (f", {self.water_m3:.2f} m3 water" if self.water_m3 else "")
            )
        elif self.pipes_used:
            lines.append("  route length: unknown - no length_m for "
                         + ", ".join(self.unmeasured_pipes))
        if self.pattern_note:
            lines.append(f"  {self.pattern_note}")
        for sb in self.multi_bus_switchboards:
            hdrs = ", ".join(b.header for b in self.buses if b.switchboard == sb)
            lines.append(f"  note: {sb} used as separate buses on {hdrs}")

        for c in self.circuits:
            lines.append(f"  circuit {c.label}:")
            for bi in c.buses:
                b = self.buses[bi]
                names = ", ".join(self.infra.terminals[t].name for t in b.terminals)
                lines.append(f"      {b.id:<22} {names}")
            if c.supplies:
                lines.append("      supply : " + ", ".join(
                    f"{self.infra.terminals[t].name} @{self.infra.terminals[t].switchboard}"
                    for t in c.supplies))
            if c.loads:
                lines.append("      load   : " + ", ".join(
                    f"{self.infra.terminals[t].name} @{self.infra.terminals[t].switchboard}"
                    for t in c.loads))
        for a, b, p in self.hx_links:
            pipe = self.infra.pipes[p]
            if self.circuits[a].supplies and self.circuits[b].supplies:
                rel = f"couples circuit {self.circuits[a].label} and {self.circuits[b].label}"
            else:
                rel = f"circuit {self.circuits[a].label} -> circuit {self.circuits[b].label}"
            lines.append(f"  heat exchanger on pipe {p} at {pipe.hx_at}: {rel}")
        if valves:
            plan = self.valve_plan()
            lines.append("  valve plan:")
            for item in plan["open"]:
                lines.append(f"      OPEN  {item}")
            for item in plan["heat_exchangers"]:
                lines.append(f"      HX    {item}")
        return "\n".join(lines)

    def valve_plan(self, full: bool = False) -> dict:
        open_cmds: List[str] = []
        close_cmds: List[str] = []
        hx_cmds: List[str] = []

        active_by_sb: Dict[str, Set[str]] = {}
        for tid, bi in self.bus_of_terminal.items():
            active_by_sb.setdefault(self.infra.terminals[tid].switchboard, set()).add(tid)

        for tid, bi in sorted(self.bus_of_terminal.items()):
            bus = self.buses[bi]
            term = self.infra.terminals[tid]
            for rail in ("F", "R"):
                open_cmds.append(f"{term.switchboard}/{term.name}/{bus.header}.{rail}")

        for sb, active in sorted(active_by_sb.items()):
            board = self.infra.switchboards[sb]
            for tid in sorted(active):
                term = board.terminals[tid]
                mine = self.buses[self.bus_of_terminal[tid]].header
                for hname, hobj in board.headers.items():
                    for rail in hobj.rails:
                        if hname == mine and rail in ("F", "R"):
                            continue
                        close_cmds.append(f"{sb}/{term.name}/{hname}.{rail}")

        for p in self.pipes_used:
            pipe = self.infra.pipes[p]
            if self.pipe_modes[p] == "hx":
                hx_cmds.append(f"{pipe.hx_at}/HX-{p}: THROUGH  (bypass valves closed)")
            else:
                where = pipe.hx_at or pipe.ends[0]
                hx_cmds.append(f"{where}/HX-{p}: BYPASS   (exchanger isolated)")

        return {"open": sorted(set(open_cmds)),
                "close": sorted(set(close_cmds)),
                "heat_exchangers": hx_cmds}

    def to_dict(self) -> dict:
        return {
            "signature": self.signature,
            "trench_m": self.trench_m,
            "pipe_m": self.pipe_m,
            "water_m3": self.water_m3,
            "pipes": {p: self.pipe_modes[p] for p in self.pipes_used},
            "pipe_details": {
                p: {
                    "ends": list(self.infra.pipes[p].ends),
                    "hx_at": self.infra.pipes[p].hx_at,
                    "run": self.infra.pipes[p].run,
                    "terminal": self.infra.pipes[p].terminal,
                    "length_m": self.infra.pipes[p].length_m,
                    "buses": [self.buses[self.bus_of_terminal[
                        self.infra.pipe_terminal_id(self.infra.pipes[p], sb)]].id
                        for sb in self.infra.pipes[p].ends],
                }
                for p in self.pipes_used
            },
            "supplies": list(self.supply_ids),
            "loads": list(self.load_ids),
            "header_variants": self.n_header_variants,
            "load_pattern": self.load_pattern,
            "load_order": list(self.load_order),
            "buses": [
                {
                    "id": b.id,
                    "switchboard": b.switchboard,
                    "header": b.header,
                    "terminals": list(b.terminals),
                    "circuit": self.circuits[self.circuit_of_bus[i]].label,
                }
                for i, b in enumerate(self.buses)
            ],
            "circuits": [
                {
                    "label": c.label,
                    "buses": [self.buses[i].id for i in c.buses],
                    "switchboards": sorted({self.buses[i].switchboard for i in c.buses}),
                    "supplies": list(c.supplies),
                    "loads": list(c.loads),
                    "internal_pipes": list(c.pipes_internal),
                    "hx_pipes": list(c.pipes_hx),
                }
                for c in self.circuits
            ],
            "hx_links": [
                {"from": self.circuits[a].label, "to": self.circuits[b].label, "pipe": p}
                for a, b, p in self.hx_links
            ],
            "valve_plan": self.valve_plan(),
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
class _UF:
    def __init__(self, items: Iterable):
        self.p = {i: i for i in items}

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _connected(nodes: Set[str], edges: Sequence[Tuple[str, str]]) -> bool:
    if not nodes:
        return False
    uf = _UF(nodes)
    for a, b in edges:
        uf.union(a, b)
    return len({uf.find(n) for n in nodes}) == 1


def _partitions(items: Sequence[str], max_blocks: int) -> Iterable[List[List[str]]]:
    """All ways of splitting `items` into at most max_blocks non-empty groups."""
    if not items:
        yield []
        return
    first, rest = items[0], items[1:]
    for sub in _partitions(rest, max_blocks):
        for i in range(len(sub)):
            yield sub[:i] + [[first] + sub[i]] + sub[i + 1:]
        if len(sub) < max_blocks:
            yield [[first]] + sub


def _header_assignments(infra: Infrastructure, sb: str,
                        blocks: List[List[str]]) -> List[Tuple[str, ...]]:
    """Distinct headers for each block, respecting per-terminal restrictions."""
    options: List[Tuple[str, ...]] = []
    for blk in blocks:
        allowed = set(infra.switchboards[sb].circuit_headers)
        for tid in blk:
            allowed &= set(infra.allowed_headers(tid))
        if not allowed:
            return []
        options.append(tuple(h for h in infra.switchboards[sb].circuit_headers
                             if h in allowed))
    out: List[Tuple[str, ...]] = []
    for combo in itertools.product(*options):
        if len(set(combo)) == len(combo):        # headers must be distinct
            out.append(combo)
    return out


# --------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------
def enumerate_networks(
    infra: Infrastructure,
    supplies: Sequence[str],
    loads: Sequence[str],
    *,
    allow_hx: bool = True,
    allow_bypass: bool = True,
    allow_cycles: bool = False,
    require_pipes: Sequence[str] = (),
    forbid_pipes: Sequence[str] = (),
    check_roles: bool = True,
    max_results: Optional[int] = None,
    max_extra_pipes: int = 1,
    load_topology: str = "any",
    allow_redundant: bool = False,
) -> List[Solution]:
    """Every feasible topology serving `loads` from `supplies`."""

    supply_ids = tuple(infra.resolve(s) for s in supplies)
    load_ids = tuple(infra.resolve(l) for l in loads)

    if check_roles:
        for t in supply_ids:
            if infra.terminals[t].role not in SOURCE_ROLES:
                raise ValueError(f"{t} is not a heat source (role={infra.terminals[t].role})")
        for t in load_ids:
            if infra.terminals[t].role not in SINK_ROLES:
                raise ValueError(f"{t} is not a heat load (role={infra.terminals[t].role})")
    overlap = set(supply_ids) & set(load_ids)
    if overlap:
        raise ValueError(f"terminal listed as both supply and load: {sorted(overlap)}")

    if load_topology not in ("any", "series", "parallel"):
        raise ValueError("load_topology must be 'any', 'series' or 'parallel'")

    required = {infra.terminals[t].switchboard for t in supply_ids + load_ids}
    candidates = [p for p in infra.pipes if p not in set(forbid_pipes)]
    require_set = set(require_pipes)
    if not require_set <= set(candidates):
        raise ValueError("required pipe is also forbidden / unknown")

    solutions: List[Solution] = []

    for r in range(len(candidates) + 1):
        for subset in itertools.combinations(candidates, r):
            if not require_set <= set(subset):
                continue

            nodes: Set[str] = set(required)
            edges: List[Tuple[str, str]] = []
            for p in subset:
                nodes.update(infra.pipes[p].ends)
                edges.append(infra.pipes[p].ends)
            if not _connected(nodes, edges):
                continue
            # A tree spanning these switchboards needs len(nodes)-1 pipes.
            # Allow a few more for loop-backs, but not the whole power set -
            # without this the partition search below explodes.
            if len(subset) > len(nodes) - 1 + max_extra_pipes:
                continue

            # A switchboard holding neither a supply nor a load is only worth
            # entering if the route passes THROUGH it to somewhere else. Going
            # out to an empty building and straight back (K1 out, K2 back) is a
            # pointless detour, not transit.
            if not allow_redundant:
                nbrs: Dict[str, Set[str]] = {n: set() for n in nodes}
                for a, b in edges:
                    nbrs[a].add(b)
                    nbrs[b].add(a)
                if any(len(nbrs[n]) < 2 for n in nodes - required):
                    continue

            # terminals that must be valved on, grouped by switchboard
            active: Dict[str, List[str]] = {sb: [] for sb in nodes}
            for t in supply_ids + load_ids:
                active[infra.terminals[t].switchboard].append(t)
            for p in subset:
                for sb in infra.pipes[p].ends:
                    active[sb].append(infra.pipe_terminal_id(infra.pipes[p], sb))
            active = {sb: sorted(set(v)) for sb, v in active.items()}
            if any(not v for v in active.values()):
                continue

            # ways of grouping each switchboard's terminals onto buses
            per_sb: Dict[str, List[List[List[str]]]] = {}
            ok = True
            for sb, terms in active.items():
                cap = len(infra.switchboards[sb].circuit_headers)
                parts = [pt for pt in _partitions(terms, cap)
                         if _header_assignments(infra, sb, pt)]
                if not parts:
                    ok = False
                    break
                per_sb[sb] = parts
            if not ok:
                continue

            boards = sorted(per_sb)
            for combo in itertools.product(*[per_sb[sb] for sb in boards]):
                for mode_combo in itertools.product(
                    *[infra.pipes[p].modes(allow_hx, allow_bypass) for p in subset]
                ):
                    sol = _build(infra, subset, dict(zip(subset, mode_combo)),
                                 dict(zip(boards, combo)), supply_ids, load_ids,
                                 allow_cycles, allow_redundant)
                    if sol is not None and load_topology != "any" \
                            and sol.load_pattern not in (load_topology, "single"):
                        sol = None
                    if sol is not None:
                        solutions.append(sol)
                        if max_results and len(solutions) >= max_results:
                            solutions.sort(key=lambda s: s.cost_key)
                            return solutions

    solutions.sort(key=lambda s: s.cost_key)
    return solutions


def _build(infra, subset, mode_of, blocks_by_sb, supply_ids, load_ids,
           allow_cycles, allow_redundant=False) -> Optional[Solution]:
    # ---- 1. buses ---------------------------------------------------------
    buses: List[Bus] = []
    bus_of_terminal: Dict[str, int] = {}
    variants = 1
    for sb in sorted(blocks_by_sb):
        blocks = blocks_by_sb[sb]
        assigns = _header_assignments(infra, sb, blocks)
        if not assigns:
            return None
        variants *= len(assigns)
        chosen = assigns[0]                       # canonical labelling
        for hdr, blk in zip(chosen, blocks):
            idx = len(buses)
            buses.append(Bus(sb, hdr, tuple(sorted(blk))))
            for tid in blk:
                bus_of_terminal[tid] = idx

    # ---- 2. hydraulic circuits: merge buses joined by a bypassed pipe -----
    uf = _UF(range(len(buses)))
    bus_edges: List[Tuple[int, int]] = []
    for p in subset:
        pipe = infra.pipes[p]
        a = bus_of_terminal[infra.pipe_terminal_id(pipe, pipe.ends[0])]
        b = bus_of_terminal[infra.pipe_terminal_id(pipe, pipe.ends[1])]
        bus_edges.append((a, b))
        if mode_of[p] == "bypass":
            uf.union(a, b)

    if not allow_cycles:
        seen_pairs = set()
        for a, b in bus_edges:
            key = (min(a, b), max(a, b))
            if key in seen_pairs:
                return None                       # parallel runs on the same buses
            seen_pairs.add(key)

    roots = sorted({uf.find(i) for i in range(len(buses))})
    idx_of_root = {rt: i for i, rt in enumerate(roots)}
    circuit_of_bus = {i: idx_of_root[uf.find(i)] for i in range(len(buses))}

    # every bus graph must be connected overall (no detached island)
    if not _connected(set(range(len(buses))), bus_edges) and len(buses) > 1:
        return None

    # No dead-end branches and no pointless loops: every pipe must be
    # load-bearing, i.e. taking it out must cut at least one load off from
    # every supply. A branch to a bus with nothing on it fails this, and so
    # does a loop whose loads are still reachable the other way round.
    if not allow_redundant and subset:
        supply_buses = {bus_of_terminal[t] for t in supply_ids}
        load_buses = {bus_of_terminal[t] for t in load_ids}
        for k in range(len(bus_edges)):
            rest = bus_edges[:k] + bus_edges[k + 1:]
            reach = set(supply_buses)
            frontier = list(reach)
            nbr: Dict[int, List[int]] = {}
            for a, b in rest:
                nbr.setdefault(a, []).append(b)
                nbr.setdefault(b, []).append(a)
            while frontier:
                cur = frontier.pop()
                for nxt in nbr.get(cur, ()):
                    if nxt not in reach:
                        reach.add(nxt)
                        frontier.append(nxt)
            if load_buses <= reach:
                return None            # this pipe carries nothing essential

    # ---- 3. exchangers ----------------------------------------------------
    hx_raw: List[Tuple[int, int, str]] = []
    for p in subset:
        if mode_of[p] == "hx":
            pipe = infra.pipes[p]
            a = circuit_of_bus[bus_of_terminal[infra.pipe_terminal_id(pipe, pipe.ends[0])]]
            b = circuit_of_bus[bus_of_terminal[infra.pipe_terminal_id(pipe, pipe.ends[1])]]
            if a == b:
                return None                       # short-circuited exchanger
            hx_raw.append((a, b, p))

    # ---- 4. thermal reachability -----------------------------------------
    n_circ = len(roots)
    adj: Dict[int, List[Tuple[int, str]]] = {i: [] for i in range(n_circ)}
    for a, b, p in hx_raw:
        adj[a].append((b, p))
        adj[b].append((a, p))

    circ_of_terminal = {t: circuit_of_bus[bus_of_terminal[t]]
                        for t in supply_ids + load_ids}
    supply_circuits = {circ_of_terminal[t] for t in supply_ids}
    load_circuits = {circ_of_terminal[t] for t in load_ids}

    reached = set(supply_circuits)
    order = sorted(supply_circuits)
    oriented: List[Tuple[int, int, str]] = []
    frontier = list(order)
    while frontier:
        cur = frontier.pop(0)
        for nxt, p in sorted(adj[cur]):
            if nxt not in reached:
                reached.add(nxt)
                order.append(nxt)
                oriented.append((cur, nxt, p))
                frontier.append(nxt)
    if not load_circuits <= reached or len(reached) != n_circ:
        return None

    # ---- 5. no dead leaf --------------------------------------------------
    for i in range(n_circ):
        if len(adj[i]) <= 1 and i not in supply_circuits and i not in load_circuits:
            return None

    # ---- 6. relabel in heat-flow order -----------------------------------
    relabel = {old: new for new, old in enumerate(order)}
    covered = {p for _, _, p in oriented}
    for a, b, p in hx_raw:
        if p not in covered:
            oriented.append((a, b, p))
    circuit_of_bus = {i: relabel[c] for i, c in circuit_of_bus.items()}
    oriented = [(relabel[a], relabel[b], p) for a, b, p in oriented]

    circuits: List[Circuit] = []
    for i in range(n_circ):
        mine = tuple(bi for bi in range(len(buses)) if circuit_of_bus[bi] == i)
        circuits.append(Circuit(
            index=i,
            buses=mine,
            supplies=tuple(t for t in supply_ids
                           if circuit_of_bus[bus_of_terminal[t]] == i),
            loads=tuple(t for t in load_ids
                        if circuit_of_bus[bus_of_terminal[t]] == i),
            pipes_internal=tuple(
                p for p in subset if mode_of[p] == "bypass"
                and circuit_of_bus[bus_of_terminal[
                    infra.pipe_terminal_id(infra.pipes[p], infra.pipes[p].ends[0])]] == i),
            pipes_hx=tuple(
                p for p in subset if mode_of[p] == "hx" and i in (
                    circuit_of_bus[bus_of_terminal[
                        infra.pipe_terminal_id(infra.pipes[p], infra.pipes[p].ends[0])]],
                    circuit_of_bus[bus_of_terminal[
                        infra.pipe_terminal_id(infra.pipes[p], infra.pipes[p].ends[1])]])),
        ))

    pattern, order = _load_pattern(infra, buses, bus_of_terminal,
                                   subset, supply_ids, load_ids)

    return Solution(
        infra=infra,
        pipes_used=tuple(subset),
        pipe_modes=mode_of,
        buses=buses,
        circuits=circuits,
        bus_of_terminal=bus_of_terminal,
        circuit_of_bus=circuit_of_bus,
        hx_links=tuple(oriented),
        supply_ids=supply_ids,
        load_ids=load_ids,
        n_header_variants=variants,
        load_pattern=pattern,
        load_order=order,
    )


def _load_pattern(infra, buses, bus_of_terminal, subset, supply_ids, load_ids):
    """Are the loads chained one after another, or branching in parallel?

    Walks outward from the supply buses over every pipe (heat crosses an
    exchanger too) and asks, for each load, how many other loads lie upstream
    of it. None upstream of any -> parallel. A single chain -> series.
    """
    if len(load_ids) < 2:
        return "single", tuple(load_ids)

    adj: Dict[int, List[int]] = {i: [] for i in range(len(buses))}
    for p in subset:
        pipe = infra.pipes[p]
        a = bus_of_terminal[infra.pipe_terminal_id(pipe, pipe.ends[0])]
        b = bus_of_terminal[infra.pipe_terminal_id(pipe, pipe.ends[1])]
        adj[a].append(b)
        adj[b].append(a)

    seeds = [bus_of_terminal[t] for t in supply_ids]
    parent: Dict[int, Optional[int]] = {s: None for s in seeds}
    frontier = list(dict.fromkeys(seeds))
    while frontier:
        cur = frontier.pop(0)
        for nxt in adj[cur]:
            if nxt not in parent:
                parent[nxt] = cur
                frontier.append(nxt)

    load_bus = {t: bus_of_terminal[t] for t in load_ids}
    bus_loads: Dict[int, List[str]] = {}
    for t, b in load_bus.items():
        bus_loads.setdefault(b, []).append(t)

    def ancestors(b: int) -> Set[int]:
        out: Set[int] = set()
        cur = parent.get(b)
        while cur is not None:
            out.add(cur)
            cur = parent.get(cur)
        return out

    upstream: Dict[str, Set[str]] = {}
    for t, b in load_bus.items():
        anc = ancestors(b)
        same_bus = [o for o in bus_loads[b] if o != t]      # same bus = parallel
        upstream[t] = {o for o in load_ids
                       if o != t and load_bus[o] in anc and o not in same_bus}

    if all(not v for v in upstream.values()):
        return "parallel", ()

    order = sorted(load_ids, key=lambda t: len(upstream[t]))
    if all(len(upstream[t]) == k for k, t in enumerate(order)):
        return "series", tuple(order)
    return "mixed", ()


def can_coexist(a: Solution, b: Solution) -> bool:
    """True if two networks can run at the same time on the physical plant."""
    if set(a.pipes_used) & set(b.pipes_used):
        return False
    if set(a.supply_ids + a.load_ids) & set(b.supply_ids + b.load_ids):
        return False
    used_a = {bus.id for bus in a.buses}
    used_b = {bus.id for bus in b.buses}
    return not (used_a & used_b)        # buses are exclusive


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _print_inventory(infra: Infrastructure) -> None:
    for sb_name, sb in infra.switchboards.items():
        hdr = ", ".join(f"{h}{'' if o.is_circuit else ' (bypass rail)'}"
                        for h, o in sb.headers.items())
        print(f"\n{sb_name}  [building {sb.building}]  headers: {hdr}")
        for tid, t in sb.terminals.items():
            tag = f"pipe {t.pipe}" if t.role == "pipe" else t.role
            lim = f"   [only {', '.join(t.headers)}]" if t.headers else ""
            print(f"    {t.name:<18} {tag}{lim}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topology", type=Path, default=DEFAULT_TOPOLOGY)
    ap.add_argument("--supply", nargs="+", default=[])
    ap.add_argument("--load", nargs="+", default=[])
    ap.add_argument("--no-hx", action="store_true")
    ap.add_argument("--no-bypass", action="store_true")
    ap.add_argument("--cycles", action="store_true",
                    help="allow two runs between the SAME pair of buses")
    ap.add_argument("--require-pipe", nargs="*", default=[])
    ap.add_argument("--forbid-pipe", nargs="*", default=[])
    ap.add_argument("--no-role-check", action="store_true")
    ap.add_argument("--sort", choices=["simple", "length"], default="simple")
    ap.add_argument("--max", type=int, default=None)
    ap.add_argument("--allow-redundant", action="store_true",
                    help="keep dead-end branches and spare parallel paths")
    ap.add_argument("--loads-in", choices=["any", "series", "parallel"],
                    default="any", help="require the loads to be chained or branching")
    ap.add_argument("--extra-pipes", type=int, default=1,
                    help="pipes allowed beyond a spanning tree (default 1)")
    ap.add_argument("--valves", action="store_true")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    infra = Infrastructure.load(args.topology)

    if args.list or not (args.supply and args.load):
        _print_inventory(infra)
        if not (args.supply and args.load):
            print("\ngive --supply and --load to generate networks")
        return 0

    try:
        sols = enumerate_networks(
            infra, args.supply, args.load,
            allow_hx=not args.no_hx,
            allow_bypass=not args.no_bypass,
            allow_cycles=args.cycles,
            require_pipes=args.require_pipe,
            forbid_pipes=args.forbid_pipe,
            check_roles=not args.no_role_check,
            max_results=args.max,
            max_extra_pipes=args.extra_pipes,
            load_topology=args.loads_in,
            allow_redundant=args.allow_redundant,
        )
    except (KeyError, ValueError) as exc:
        print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 2

    if args.sort == "length":
        sols.sort(key=lambda s: s.length_key)

    print(f"supplies : {', '.join(infra.resolve(s) for s in args.supply)}")
    print(f"loads    : {', '.join(infra.resolve(l) for l in args.load)}")
    print(f"{len(sols)} feasible topology/topologies\n")
    for i, s in enumerate(sols, 1):
        print(f"--- network {i} " + "-" * 55)
        print(s.describe(valves=args.valves))
        print()

    if args.json:
        out = []
        for n, s in enumerate(sols, 1):
            d = s.to_dict()
            d["id"] = f"N{n:03d}"
            out.append(d)
        args.json.write_text(json.dumps(out, indent=2))
        print(f"written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
