#!/usr/bin/env python
"""
draw_network.py  -  draw one network from a saved JSON file.

The JSON comes from the generator:

    py -3 dh_network_generator.py --supply CHP --load "Flexhouse 3" --json networks.json

Then:

    py -3 draw_network.py networks.json --list          # index + one-line summary
    py -3 draw_network.py networks.json -n 5            # draw network 5
    py -3 draw_network.py networks.json -n 5 -o fig.svg
    py -3 draw_network.py networks.json --all -d figs   # draw every network

Indices are 1-based and match the numbering printed by the generator. The
output is a plain SVG file - no dependencies, opens in any browser and in
Inkscape.

Drawing conventions
-------------------
    box            one switchboard, tinted by the hydraulic circuit it belongs
                   to, labelled with its header
    up triangle    heat source valved onto that header
    down triangle  load valved onto that header
    red line       forward rail (F), arrow shows the direction away from supply
    blue line      return rail (R)
    plain crossing pipe bypassing its exchanger - one hydraulic circuit
    HX block       pipe routed through its exchanger - two separate circuits
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Site layout: relative positions of the switchboards, mirroring the overview
# drawing (310-T is the hub). Unknown boards fall back to a ring.
# --------------------------------------------------------------------------
LAYOUT: Dict[str, Tuple[float, float]] = {
    "310-D": (-1.30, 0.00),
    "310-T": (0.00, 0.00),
    "117-D": (1.35, -0.95),
    "330-D": (1.35, 0.95),
    "716-D": (0.00, 1.60),
}

CIRCUIT_FILL = ["#dce8f4", "#e3f0dd", "#f6e2ec", "#fbf0d5", "#e6e2f2", "#f0e6dd"]
CIRCUIT_EDGE = ["#5c86ad", "#6d9464", "#b0779a", "#c2a martin", "#8579a8", "#a08876"]
CIRCUIT_EDGE = ["#5c86ad", "#6d9464", "#b0779a", "#c2a24a", "#8579a8", "#a08876"]

FORWARD = "#c0392b"
RETURN = "#2c5aa0"
INK = "#1a1a1a"
MUTED = "#666666"

SCALE_X, SCALE_Y = 300.0, 210.0
MARGIN_X, MARGIN_TOP = 130.0, 122.0
BOX_W = 190.0
ROW_H = 17.0
RAIL_GAP = 5.5


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def short(term_id: str) -> str:
    return term_id.split("::", 1)[-1]


def board_of(term_id: str) -> str:
    return term_id.split("::", 1)[0]


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------
def rect_exit(cx: float, cy: float, w: float, h: float,
              tx: float, ty: float) -> Tuple[float, float]:
    """Point where the line from (cx,cy) to (tx,ty) leaves the box."""
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
    sx = (w / 2) / abs(dx) if dx else float("inf")
    sy = (h / 2) / abs(dy) if dy else float("inf")
    s = min(sx, sy)
    return cx + dx * s, cy + dy * s


def unit(ax: float, ay: float, bx: float, by: float) -> Tuple[float, float, float]:
    dx, dy = bx - ax, by - ay
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    return dx / length, dy / length, length


# --------------------------------------------------------------------------
# Model pulled out of the JSON
# --------------------------------------------------------------------------
class NetworkDrawing:
    def __init__(self, sol: dict, index: int, total: int):
        self.sol = sol
        self.index = index
        self.total = total

        self.pipes: Dict[str, str] = sol["pipes"]
        self.details: Dict[str, dict] = sol.get("pipe_details", {})
        if self.pipes and not self.details:
            raise SystemExit(
                "this JSON predates pipe_details - regenerate it with the "
                "current dh_network_generator.py"
            )
        self.circuits: List[dict] = sol["circuits"]
        self.bus_list: List[dict] = sol.get("buses", [])
        if not self.bus_list:
            raise SystemExit(
                "this JSON predates the bus model - regenerate it with the "
                "current dh_network_generator.py"
            )

        self.buses = {b["id"]: b for b in self.bus_list}
        self.nodes: List[str] = [b["id"] for b in self.bus_list]
        self.circuit_index = {c["label"]: i for i, c in enumerate(self.circuits)}
        self.circuit_of = {b["id"]: self.circuit_index[b["circuit"]]
                           for b in self.bus_list}

        supplies = set(sol.get("supplies", []))
        loads = set(sol.get("loads", []))
        self.supplies: Dict[str, List[str]] = {}
        self.loads: Dict[str, List[str]] = {}
        for b in self.bus_list:
            self.supplies[b["id"]] = [short(t) for t in b["terminals"] if t in supplies]
            self.loads[b["id"]] = [short(t) for t in b["terminals"] if t in loads]

        self.pos = self._positions()
        self.flow = self._orient_flow()

    def pipe_buses(self, name: str) -> Tuple[str, str]:
        d = self.details[name]
        if d.get("buses"):
            return tuple(d["buses"])                       # type: ignore
        return (d["ends"][0], d["ends"][1])

    # -- placement ---------------------------------------------------------
    def _positions(self) -> Dict[str, Tuple[float, float]]:
        import math

        by_sb: Dict[str, List[str]] = {}
        for b in self.bus_list:
            by_sb.setdefault(b["switchboard"], []).append(b["id"])

        raw: Dict[str, Tuple[float, float]] = {}
        unknown = [sb for sb in by_sb if sb not in LAYOUT]
        for sb, ids in by_sb.items():
            if sb in LAYOUT:
                bx, by = LAYOUT[sb]
            else:
                ang = 2 * math.pi * unknown.index(sb) / max(1, len(unknown))
                bx, by = 1.8 * math.cos(ang), 1.8 * math.sin(ang)
            # several buses at one switchboard: stack them vertically
            for k, bid in enumerate(sorted(ids)):
                off = (k - (len(ids) - 1) / 2) * 0.62
                raw[bid] = (bx, by + off)

        xs = [p[0] for p in raw.values()]
        ys = [p[1] for p in raw.values()]
        x0, y0 = min(xs), min(ys)
        return {
            n: (MARGIN_X + (p[0] - x0) * SCALE_X, MARGIN_TOP + (p[1] - y0) * SCALE_Y)
            for n, p in raw.items()
        }

    def box_height(self, node: str) -> float:
        rows = len(self.supplies.get(node, [])) + len(self.loads.get(node, []))
        return max(58.0, 44.0 + rows * ROW_H)

    def canvas(self) -> Tuple[float, float]:
        w = max(x for x, _ in self.pos.values()) + BOX_W / 2 + MARGIN_X
        h = (max(y + self.box_height(b) / 2 for b, (x, y) in self.pos.items())
             + 40 + 26 * (len(self.circuits) + 4))
        return w, h

    # -- flow direction ----------------------------------------------------
    def _orient_flow(self) -> Dict[str, Tuple[str, str]]:
        """For each pipe, (from_bus, to_bus) walking away from the supplies."""
        adj: Dict[str, List[Tuple[str, str]]] = {n: [] for n in self.nodes}
        for p in self.details:
            a, b = self.pipe_buses(p)
            adj.setdefault(a, []).append((b, p))
            adj.setdefault(b, []).append((a, p))

        seeds = [n for n in self.nodes if self.supplies.get(n)]
        seen = set(seeds)
        out: Dict[str, Tuple[str, str]] = {}
        frontier = list(seeds)
        while frontier:
            cur = frontier.pop(0)
            for nxt, p in adj.get(cur, []):
                if p not in out:
                    out[p] = (cur, nxt)
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        for p in self.details:
            a, b = self.pipe_buses(p)
            out.setdefault(p, (a, b))
        return out

    # -- svg ---------------------------------------------------------------
    def render(self) -> str:
        w, h = self.canvas()
        s: List[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" '
            f'height="{h:.0f}" viewBox="0 0 {w:.0f} {h:.0f}" '
            'font-family="Helvetica,Arial,sans-serif">',
            f'<rect width="{w:.0f}" height="{h:.0f}" fill="#ffffff"/>',
            '<defs>',
            f'<marker id="fwd" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{FORWARD}"/></marker>',
            '</defs>',
        ]
        s += self._title()
        s += self._edges()
        s += self._boxes()
        s += self._legend(h)
        s.append("</svg>")
        return "\n".join(s)

    def _title(self) -> List[str]:
        sup = ", ".join(short(t) for t in self.sol.get("supplies", []))
        lod = ", ".join(short(t) for t in self.sol.get("loads", []))
        pipes = ", ".join(f"{p} [{m}]" for p, m in sorted(self.pipes.items())) or "no pipes"
        pat = self.sol.get("load_pattern")
        if pat == "series" and self.sol.get("load_order"):
            pipes += "   \u00b7   series: " + " \u2192 ".join(
                short(t) for t in self.sol["load_order"])
        elif pat in ("parallel", "mixed"):
            pipes += f"   \u00b7   loads {pat}"
        if self.sol.get("trench_m"):
            pipes += (f"   \u00b7   {self.sol['trench_m']:.0f} m trench, "
                      f"{self.sol['pipe_m']:.0f} m pipe")
        return [
            f'<text x="{MARGIN_X - BOX_W/2:.0f}" y="34" font-size="19" '
            f'font-weight="bold" fill="{INK}">'
            f'{esc(self.sol.get("id") or f"N{self.index:03d}")} '
            f'<tspan font-size="13" font-weight="normal" fill="{MUTED}">'
            f'({self.index} of {self.total}, id {esc(self.sol.get("signature", "-"))})'
            f'</tspan></text>',
            f'<text x="{MARGIN_X - BOX_W/2:.0f}" y="55" font-size="12.5" fill="{MUTED}">'
            f'{esc(sup)} &#8594; {esc(lod)}</text>',
            f'<text x="{MARGIN_X - BOX_W/2:.0f}" y="72" font-size="12.5" fill="{MUTED}">'
            f'{esc(pipes)}</text>',
        ]

    def _edges(self) -> List[str]:
        out: List[str] = []
        # group parallel runs between the same pair so they do not overlap
        groups: Dict[Tuple[str, str], List[str]] = {}
        for p in self.details:
            key = tuple(sorted(self.pipe_buses(p)))
            groups.setdefault(key, []).append(p)

        for key, plist in groups.items():
            for k, p in enumerate(sorted(plist)):
                d = self.details[p]
                a, b = self.flow[p]
                ax, ay = self.pos[a]
                bx, by = self.pos[b]
                ux, uy, _ = unit(ax, ay, bx, by)
                nx, ny = -uy, ux
                lane = (k - (len(plist) - 1) / 2) * 30.0
                ax, ay = ax + nx * lane, ay + ny * lane
                bx, by = bx + nx * lane, by + ny * lane
                sx, sy = rect_exit(*self.pos[a], BOX_W, self.box_height(a),
                                   bx, by)
                ex, ey = rect_exit(*self.pos[b], BOX_W, self.box_height(b),
                                   ax, ay)
                sx, sy = sx + nx * lane, sy + ny * lane
                ex, ey = ex + nx * lane, ey + ny * lane
                out += self._one_pipe(p, d, sx, sy, ex, ey, nx, ny)
        return out

    def _one_pipe(self, name: str, d: dict, sx: float, sy: float,
                  ex: float, ey: float, nx: float, ny: float) -> List[str]:
        # stagger labels along the run so near-parallel pipes don't collide
        along = 0.38 + 0.24 * (sorted(self.details).index(name) % 2)
        mode = self.pipes[name]
        o = RAIL_GAP
        fx1, fy1, fx2, fy2 = sx + nx * o, sy + ny * o, ex + nx * o, ey + ny * o
        rx1, ry1, rx2, ry2 = sx - nx * o, sy - ny * o, ex - nx * o, ey - ny * o
        mx, my = (sx + ex) / 2, (sy + ey) / 2
        out: List[str] = []

        if mode == "hx":
            ux, uy, ln = unit(sx, sy, ex, ey)
            half = 15.0
            # split each rail around the exchanger block
            for (x1, y1, x2, y2, col) in (
                (fx1, fy1, fx2, fy2, FORWARD),
                (rx1, ry1, rx2, ry2, RETURN),
            ):
                bx1, by1 = mx + nx * (o if col == FORWARD else -o) - ux * half, \
                           my + ny * (o if col == FORWARD else -o) - uy * half
                bx2, by2 = mx + nx * (o if col == FORWARD else -o) + ux * half, \
                           my + ny * (o if col == FORWARD else -o) + uy * half
                marker = ' marker-end="url(#fwd)"' if col == FORWARD else ""
                out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{bx1:.1f}" '
                           f'y2="{by1:.1f}" stroke="{col}" stroke-width="2.6"{marker}/>')
                out.append(f'<line x1="{bx2:.1f}" y1="{by2:.1f}" x2="{x2:.1f}" '
                           f'y2="{y2:.1f}" stroke="{col}" stroke-width="2.6"/>')
            # exchanger block
            hw, hh = 15.0, 13.0
            ang = 0.0
            import math
            ang = math.degrees(math.atan2(uy, ux))
            out.append(
                f'<g transform="translate({mx:.1f},{my:.1f}) rotate({ang:.1f})">'
                f'<rect x="{-hw:.1f}" y="{-hh:.1f}" width="{2*hw:.1f}" '
                f'height="{2*hh:.1f}" rx="2" fill="#ffffff" stroke="{INK}" '
                f'stroke-width="1.8"/>'
                f'<line x1="{-hw:.1f}" y1="{hh:.1f}" x2="{hw:.1f}" y2="{-hh:.1f}" '
                f'stroke="{INK}" stroke-width="1.6"/></g>'
            )
            label = f"{name} \u00b7 HX @ {d.get('hx_at') or ''}"
        else:
            out.append(f'<line x1="{fx1:.1f}" y1="{fy1:.1f}" x2="{fx2:.1f}" '
                       f'y2="{fy2:.1f}" stroke="{FORWARD}" stroke-width="2.6" '
                       f'marker-end="url(#fwd)"/>')
            out.append(f'<line x1="{rx1:.1f}" y1="{ry1:.1f}" x2="{rx2:.1f}" '
                       f'y2="{ry2:.1f}" stroke="{RETURN}" stroke-width="2.6"/>')
            label = f"{name} \u00b7 bypass"

        offset = 22 if mode == "hx" else 30
        if d.get("length_m"):
            label += f" \u00b7 {d['length_m']:.0f} m"
        lx, ly = mx + nx * offset, my + ny * offset
        if abs(nx) > abs(ny):                      # mostly vertical pipe run
            anchor = "end" if nx < 0 else "start"
        else:
            anchor = "middle"
        out.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="11.5" '
                   f'text-anchor="{anchor}" fill="{INK}">{esc(label)}</text>')
        return out

    def _boxes(self) -> List[str]:
        out: List[str] = []
        for b in self.nodes:
            cx, cy = self.pos[b]
            h = self.box_height(b)
            ci = self.circuit_of[b]
            fill = CIRCUIT_FILL[ci % len(CIRCUIT_FILL)]
            edge = CIRCUIT_EDGE[ci % len(CIRCUIT_EDGE)]
            x, y = cx - BOX_W / 2, cy - h / 2
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{BOX_W}" '
                       f'height="{h:.1f}" rx="7" fill="{fill}" stroke="{edge}" '
                       f'stroke-width="1.8"/>')
            bus = self.buses[b]
            out.append(f'<text x="{cx:.1f}" y="{y+19:.1f}" font-size="14" '
                       f'font-weight="bold" text-anchor="middle" fill="{INK}">'
                       f'{esc(bus["switchboard"])}</text>')
            out.append(f'<text x="{cx:.1f}" y="{y+33:.1f}" font-size="11" '
                       f'text-anchor="middle" fill="{MUTED}">'
                       f'{esc(bus["header"])} '
                       f'&#183; circuit {self.circuits[ci]["label"]}</text>')
            ty = y + 49
            for name in sorted(self.supplies.get(b, [])):
                out.append(f'<path d="M{x+14:.1f},{ty-3:.1f} l5,-8 l5,8 z" '
                           f'fill="{FORWARD}"/>')
                out.append(f'<text x="{x+28:.1f}" y="{ty:.1f}" font-size="11.5" '
                           f'fill="{INK}">{esc(name)}</text>')
                ty += ROW_H
            for name in sorted(self.loads.get(b, [])):
                out.append(f'<path d="M{x+14:.1f},{ty-11:.1f} l5,8 l5,-8 z" '
                           f'fill="{RETURN}"/>')
                out.append(f'<text x="{x+28:.1f}" y="{ty:.1f}" font-size="11.5" '
                           f'fill="{INK}">{esc(name)}</text>')
                ty += ROW_H
        return out

    def _legend(self, height: float) -> List[str]:
        x = MARGIN_X - BOX_W / 2
        y = height - 26 * (len(self.circuits) + 3)
        out = [f'<text x="{x:.0f}" y="{y:.0f}" font-size="12.5" '
               f'font-weight="bold" fill="{INK}">Hydraulic circuits</text>']
        y += 20
        for i, c in enumerate(self.circuits):
            fill = CIRCUIT_FILL[i % len(CIRCUIT_FILL)]
            edge = CIRCUIT_EDGE[i % len(CIRCUIT_EDGE)]
            out.append(f'<rect x="{x:.0f}" y="{y-10:.0f}" width="13" height="13" '
                       f'rx="2" fill="{fill}" stroke="{edge}"/>')
            boards = ", ".join(c.get("buses") or sorted(c["switchboards"]))
            out.append(f'<text x="{x+20:.0f}" y="{y:.0f}" font-size="12" '
                       f'fill="{INK}">{c["label"]}: {esc(boards)}</text>')
            y += 20
        fed = {c["label"] for c in self.circuits if c["supplies"]}
        for link in self.sol["hx_links"]:
            a, b, p = link["from"], link["to"], link["pipe"]
            if a in fed and b in fed:
                txt = (f"exchanger on pipe {p} couples circuits {a} and {b} "
                       f"(direction set by temperatures)")
            else:
                txt = f"heat transfer {a} &#8594; {b} across the exchanger on pipe {p}"
            out.append(f'<text x="{x:.0f}" y="{y:.0f}" font-size="12" '
                       f'fill="{MUTED}">{txt}</text>')
            y += 20
        out.append(f'<line x1="{x:.0f}" y1="{y-4:.0f}" x2="{x+22:.0f}" '
                   f'y2="{y-4:.0f}" stroke="{FORWARD}" stroke-width="2.6"/>')
        out.append(f'<text x="{x+30:.0f}" y="{y:.0f}" font-size="12" '
                   f'fill="{MUTED}">forward (F)</text>')
        out.append(f'<line x1="{x+120:.0f}" y1="{y-4:.0f}" x2="{x+142:.0f}" '
                   f'y2="{y-4:.0f}" stroke="{RETURN}" stroke-width="2.6"/>')
        out.append(f'<text x="{x+150:.0f}" y="{y:.0f}" font-size="12" '
                   f'fill="{MUTED}">return (R)</text>')
        out.append(f'<path d="M{x+240:.0f},{y-3:.0f} l5,-9 l5,9 z" fill="{FORWARD}"/>')
        out.append(f'<text x="{x+254:.0f}" y="{y:.0f}" font-size="12" '
                   f'fill="{MUTED}">source</text>')
        out.append(f'<path d="M{x+310:.0f},{y-12:.0f} l5,9 l5,-9 z" fill="{RETURN}"/>')
        out.append(f'<text x="{x+324:.0f}" y="{y:.0f}" font-size="12" '
                   f'fill="{MUTED}">load</text>')
        out.append(f'<rect x="{x+370:.0f}" y="{y-11:.0f}" width="20" height="13" '
                   f'rx="2" fill="#ffffff" stroke="{INK}" stroke-width="1.4"/>')
        out.append(f'<line x1="{x+370:.0f}" y1="{y+2:.0f}" x2="{x+390:.0f}" '
                   f'y2="{y-11:.0f}" stroke="{INK}" stroke-width="1.2"/>')
        out.append(f'<text x="{x+398:.0f}" y="{y:.0f}" font-size="12" '
                   f'fill="{MUTED}">heat exchanger</text>')
        return out


# --------------------------------------------------------------------------
def load_solutions(path: Path) -> Tuple[List[dict], dict]:
    """Read a networks file. Accepts the run envelope or a bare list."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "networks" in data:
        return data["networks"], data.get("run", {})
    if isinstance(data, list):
        return data, {}
    return [data], {}


def default_outdir(json_path: Path, meta: dict) -> Path:
    """figures/<run stem>/ - taken from the run metadata when present."""
    if meta.get("figures_dir"):
        return Path(meta["figures_dir"])
    return json_path.parent.parent / "figures" / json_path.stem


def resolve_ref(ref, sols: List[dict]) -> int:
    """Turn a user reference into a 1-based position.

    Accepts a position (5), an id from this run ("N005"), or a content
    signature ("3e92b40c") which stays valid across runs.
    """
    if isinstance(ref, int):
        if not 1 <= ref <= len(sols):
            raise SystemExit(f"index out of range (1..{len(sols)}): {ref}")
        return ref
    text = str(ref).strip()
    if text.isdigit():
        return resolve_ref(int(text), sols)
    for i, s in enumerate(sols, 1):
        if s.get("id", "").lower() == text.lower():
            return i
    hits = [i for i, s in enumerate(sols, 1)
            if str(s.get("signature", "")).lower().startswith(text.lower())]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit(f"no network with id or signature {text!r}")
    raise SystemExit(f"{text!r} matches several signatures - give more characters")


def label_of(sol: dict, i: int) -> str:
    """Filename stem: the run id when present, else the position."""
    return sol.get("id") or f"N{i:03d}"


def summarise(sol: dict, i: int) -> str:
    pipes = ", ".join(f"{p}[{m}]" for p, m in sorted(sol["pipes"].items())) or "none"
    trench = sol.get("trench_m")
    length = f"{trench:>6.0f} m  " if trench else ""
    tag = f"{label_of(sol, i):<5} {sol.get('signature', ''):<9}"
    return (f"{tag} {length}circuits={len(sol['circuits'])}  "
            f"hx={sum(1 for m in sol['pipes'].values() if m == 'hx')}  {pipes}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json_file", type=Path, help="file written by --json")
    ap.add_argument("-n", "--index", type=str,
                    help="which network: position (5), id (N005), or signature")
    ap.add_argument("--all", action="store_true", help="draw every network")
    ap.add_argument("-o", "--out", type=Path, help="output SVG (single network)")
    ap.add_argument("-d", "--outdir", type=Path, default=None,
                    help="output folder; defaults to figures/<run stem>/")
    ap.add_argument("--list", action="store_true", help="list the networks and exit")
    args = ap.parse_args(argv)

    sols, meta = load_solutions(args.json_file)
    total = len(sols)

    if args.list or (args.index is None and not args.all):
        if meta.get("name") or meta.get("timestamp"):
            print(f"run: {meta.get('name') or '-'}   {meta.get('timestamp', '')}")
        print(f"{total} networks in {args.json_file}\n")
        for i, s in enumerate(sols, 1):
            print(summarise(s, i))
        if args.index is None and not args.all:
            print("\nuse -n INDEX to draw one, or --all")
        return 0

    out_dir = args.outdir or default_outdir(args.json_file, meta)

    if args.all:
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, s in enumerate(sols, 1):
            path = out_dir / f"network_{label_of(s, i)}.svg"
            path.write_text(NetworkDrawing(s, i, total).render(), encoding="utf-8")
        print(f"wrote {total} files to {out_dir}")
        return 0

    idx = resolve_ref(args.index, sols)

    out = args.out or (out_dir / f"network_{label_of(sols[idx - 1], idx)}.svg")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(NetworkDrawing(sols[idx - 1], idx, total).render(),
                   encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
