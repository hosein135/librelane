"""Locate hardened views / reports inside a completed run's work tree.

The work tree is addressed through a :class:`FileSource` so the same code
serves files from disk (run still on disk) or from ``FlowRunFile`` rows in
Postgres (archived run). Paths are POSIX-style and relative to the work dir::

    sources/<uploaded>.v
    1-yosys-synthesis/<design>.nl.v
    11-openroad-fillinsertion/<design>.def
    13-openroad-stapostpnr/<corner>/<design>__<corner>.lib
    14-klayout-streamout/<design>.klayout.gds
    ...
"""

from __future__ import annotations

import fnmatch
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Protocol


class FileSource(Protocol):
    def list(self) -> list[str]:
        """POSIX relative paths of every *file* (directories excluded)."""

    def read(self, rel_path: str) -> bytes: ...


class DictFileSource:
    """In-memory source, handy for tests."""

    def __init__(self, files: dict[str, bytes | str]) -> None:
        self._files = {
            k.replace("\\", "/").lstrip("/"): (v.encode("utf-8") if isinstance(v, str) else v)
            for k, v in files.items()
        }

    def list(self) -> list[str]:
        return sorted(self._files)

    def read(self, rel_path: str) -> bytes:
        return self._files[rel_path.replace("\\", "/").lstrip("/")]


def slugify(value: str) -> str:
    """Same as ``librelane.common.misc.slugify`` (used for step directory names)."""
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s\-\.]", "", value).strip().lower()
    return re.sub(r"[-\s\.]+", "-", value)


_STEP_DIR_RE = re.compile(r"^(\d+)-([a-z0-9\-]+)$")


@dataclass
class StepRecord:
    order: int
    step_id: str
    title: str
    status: str
    output: dict[str, Any] = field(default_factory=dict)


@dataclass
class DesignInfo:
    design_name: str
    pdk: str  # LibreLane PDK variant, e.g. sky130A
    pdk_family: str  # sky130 | gf180mcu | ihp-sg13g2
    clock_period_ns: float
    clock_port: str
    run_id: int
    run_name: str
    librelane_version: str
    created_at: str  # ISO 8601
    steps: list[StepRecord] = field(default_factory=list)
    author: str = ""
    project_name: str = ""
    project_description: str = ""
    top_language: str = "Verilog"
    tool_name: str = "LibreLane Web"

    @property
    def clock_hz(self) -> int:
        if self.clock_period_ns <= 0:
            return 0
        return int(round(1e9 / self.clock_period_ns))


class WorkTree:
    """Indexed view over a :class:`FileSource`."""

    def __init__(self, source: FileSource, design_name: str) -> None:
        self.source = source
        self.design_name = design_name
        self.files = sorted(source.list())
        self.step_dirs: dict[str, str] = {}  # slug -> "<n>-<slug>" (highest n wins)
        best: dict[str, int] = {}
        for rel in self.files:
            top = rel.split("/", 1)[0]
            m = _STEP_DIR_RE.match(top)
            if not m:
                continue
            order = int(m.group(1))
            slug = m.group(2)
            if order >= best.get(slug, -1):
                best[slug] = order
                self.step_dirs[slug] = top

    # -- lookups -----------------------------------------------------------
    def read_bytes(self, rel: str) -> bytes:
        return self.source.read(rel)

    def read_text(self, rel: str) -> str:
        return self.read_bytes(rel).decode("utf-8", errors="replace")

    def exists(self, rel: str) -> bool:
        return rel in self.files

    def step_dir(self, step_id: str) -> str | None:
        return self.step_dirs.get(slugify(step_id))

    def glob(self, pattern: str) -> list[str]:
        """``fnmatch`` against the relative path (``*`` matches ``/`` too)."""
        return [f for f in self.files if fnmatch.fnmatchcase(f, pattern)]

    def in_step(self, step_id: str, pattern: str = "*") -> list[str]:
        d = self.step_dir(step_id)
        if d is None:
            return []
        prefix = d + "/"
        return [
            f for f in self.files if f.startswith(prefix) and fnmatch.fnmatchcase(f[len(prefix) :], pattern)
        ]

    def first_in_steps(self, step_ids: list[str], pattern: str) -> str | None:
        """First match, trying ``step_ids`` in order (later steps listed first)."""
        for step_id in step_ids:
            hits = self.in_step(step_id, pattern)
            if hits:
                # Prefer the design's own file when several match.
                for hit in hits:
                    if hit.rsplit("/", 1)[-1].startswith(self.design_name):
                        return hit
                return hits[0]
        return None

    def sources(self) -> list[str]:
        files = [f for f in self.files if f.startswith("sources/") and f.lower().endswith((".v", ".sv"))]
        if files:
            return files
        return [f for f in self.files if "/" not in f and f.lower().endswith((".v", ".sv"))]


@dataclass
class ResolvedView:
    kind: str  # gds, lef, def, netlist, powered_netlist, sdc, spice, spef, lib, sdf, ...
    rel_path: str
    description: str
    required: bool = True


@dataclass
class ViewSet:
    views: dict[str, ResolvedView] = field(default_factory=dict)
    multi: dict[str, list[ResolvedView]] = field(default_factory=dict)  # spef, lib, sdf, reports
    missing: list[tuple[str, str, bool]] = field(default_factory=list)  # (kind, description, required)

    def get(self, kind: str) -> ResolvedView | None:
        return self.views.get(kind)

    def missing_required(self) -> list[str]:
        return [desc for kind, desc, required in self.missing if required]


# Steps that carry a full layout snapshot, latest first.
LAYOUT_STEPS = [
    "OpenROAD.FillInsertion",
    "OpenROAD.DetailedRouting",
    "OpenROAD.GlobalRouting",
    "OpenROAD.CTS",
    "OpenROAD.DetailedPlacement",
    "OpenROAD.GlobalPlacement",
    "OpenROAD.GeneratePDN",
    "OpenROAD.IOPlacement",
    "OpenROAD.TapEndcapInsertion",
    "OpenROAD.Floorplan",
]
GDS_STEPS = ["KLayout.StreamOut", "Magic.StreamOut"]


def resolve_views(tree: WorkTree) -> ViewSet:
    vs = ViewSet()

    def single(kind: str, description: str, steps: list[str], pattern: str, required: bool = True):
        hit = tree.first_in_steps(steps, pattern)
        if hit is None:
            vs.missing.append((kind, description, required))
        else:
            vs.views[kind] = ResolvedView(kind, hit, description, required)

    def many(kind: str, description: str, steps: list[str], pattern: str, required: bool = False):
        hits: list[str] = []
        for step in steps:
            hits = tree.in_step(step, pattern)
            if hits:
                break
        if not hits:
            vs.missing.append((kind, description, required))
        else:
            vs.multi[kind] = [ResolvedView(kind, h, description, required) for h in sorted(hits)]

    single("gds", "Final GDSII layout", GDS_STEPS, "*.gds")
    # Prefer Magic.WriteLEF; fall back to final/lef or any design-named .lef
    # (older runs / archived trees may lack the WriteLEF step directory).
    lef_hit = tree.first_in_steps(["Magic.WriteLEF"], "*.lef")
    if lef_hit is None:
        candidates = [
            f
            for f in tree.files
            if f.lower().endswith(".lef")
            and (
                "/final/lef/" in f.replace("\\", "/").lower()
                or f.rsplit("/", 1)[-1].startswith(tree.design_name)
                or "writelef" in f.lower()
            )
        ]
        if candidates:
            preferred = [
                f for f in candidates if f.rsplit("/", 1)[-1].startswith(tree.design_name)
            ]
            lef_hit = (preferred or candidates)[0]
    if lef_hit is None:
        vs.missing.append(("lef", "LEF abstract (Magic.WriteLEF)", True))
    else:
        vs.views["lef"] = ResolvedView(
            "lef", lef_hit, "LEF abstract (Magic.WriteLEF)", True
        )
    single("def", "Final DEF (after fill insertion)", LAYOUT_STEPS, "*.def")
    single(
        "powered_netlist",
        "Powered gate-level netlist (*.pnl.v)",
        LAYOUT_STEPS,
        "*.pnl.v",
    )
    single(
        "netlist",
        "Gate-level netlist (*.nl.v)",
        LAYOUT_STEPS + ["Yosys.Synthesis"],
        "*.nl.v",
    )
    single("sdc", "Timing constraints (SDC)", LAYOUT_STEPS + ["Yosys.Synthesis"], "*.sdc")
    single("spice", "Extracted SPICE netlist", ["Magic.SpiceExtraction"], "*.spice", required=False)
    single("synth_netlist", "Post-synthesis netlist", ["Yosys.Synthesis"], "*.nl.v", required=False)
    many("spef", "Parasitics (SPEF, per corner)", ["OpenROAD.RCX"], "*.spef")
    many("lib", "Timing library (.lib, per corner)", ["OpenROAD.STAPostPNR"], "*.lib")
    many("sdf", "Standard delay format (.sdf, per corner)", ["OpenROAD.STAPostPNR"], "*.sdf")
    many("sta_reports", "STA reports", ["OpenROAD.STAPostPNR"], "*.rpt")
    many("drc_reports", "DRC reports", ["Magic.DRC", "KLayout.DRC"], "*.rpt")
    many("drc_xml", "DRC markers (KLayout XML)", ["Magic.DRC", "KLayout.DRC"], "*.xml")
    many("drc_json", "DRC results (JSON)", ["Magic.DRC", "KLayout.DRC"], "*.json")
    many("lvs_reports", "LVS reports", ["Netgen.LVS"], "*.rpt")
    many("lvs_json", "LVS results (JSON)", ["Netgen.LVS"], "*.json")
    many("synth_reports", "Synthesis statistics", ["Yosys.Synthesis"], "*.rpt")
    many("synth_json", "Synthesis statistics (JSON)", ["Yosys.Synthesis"], "*.json")
    # Tool logs from every step directory.
    logs = [
        f for f in tree.files if f.endswith(".log") and _STEP_DIR_RE.match(f.split("/", 1)[0])
    ]
    if logs:
        vs.multi["logs"] = [ResolvedView("logs", f, "Tool log", False) for f in logs]
    return vs


# --- Metrics ------------------------------------------------------------------


def merged_metrics(info: DesignInfo) -> dict[str, Any]:
    """Final metric values: later steps override earlier ones."""
    metrics: dict[str, Any] = {}
    for step in sorted(info.steps, key=lambda s: s.order):
        if step.status not in ("done", "skipped"):
            continue
        step_metrics = (step.output or {}).get("metrics") or {}
        if isinstance(step_metrics, dict):
            metrics.update(step_metrics)
    return metrics


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def parse_bbox(value: Any) -> tuple[float, float, float, float] | None:
    """``design__die__bbox`` is ``"x0 y0 x1 y1"`` in microns."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 4:
        nums = [_num(v) for v in value]
    else:
        parts = str(value).replace(",", " ").split()
        if len(parts) != 4:
            return None
        nums = [_num(p) for p in parts]
    if any(n is None for n in nums):
        return None
    x0, y0, x1, y1 = nums  # type: ignore[misc]
    return (x0, y0, x1, y1)


@dataclass
class MetricsSummary:
    die_bbox_um: tuple[float, float, float, float] | None
    die_width_um: float | None
    die_height_um: float | None
    die_area_um2: float | None
    core_bbox_um: tuple[float, float, float, float] | None
    instance_count: int | None
    stdcell_count: int | None
    utilization_pct: float | None
    setup_ws_ns: float | None
    hold_ws_ns: float | None
    setup_tns_ns: float | None
    hold_tns_ns: float | None
    setup_violations: int | None
    hold_violations: int | None
    max_slew_violations: int | None
    max_cap_violations: int | None
    max_fanout_violations: int | None
    drc_violations: int | None
    lvs_errors: int | None
    antenna_violations: int | None
    power_uw: float | None
    wire_length_um: float | None
    raw: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "raw"}
        return data

    def lines(self) -> list[str]:
        def fmt(v: Any, unit: str = "") -> str:
            if v is None:
                return "n/a"
            if isinstance(v, float):
                return f"{v:,.3f}".rstrip("0").rstrip(".") + unit
            return f"{v}{unit}"

        out = []
        if self.die_width_um is not None and self.die_height_um is not None:
            out.append(
                f"Die: {self.die_width_um:g} x {self.die_height_um:g} um "
                f"({(self.die_area_um2 or 0):,.0f} um^2)"
            )
        out.append(f"Instances: {fmt(self.instance_count)} (std cells: {fmt(self.stdcell_count)})")
        out.append(f"Utilization: {fmt(self.utilization_pct, ' %')}")
        out.append(
            f"Setup WS/TNS: {fmt(self.setup_ws_ns, ' ns')} / {fmt(self.setup_tns_ns, ' ns')} "
            f"(violations: {fmt(self.setup_violations)})"
        )
        out.append(
            f"Hold WS/TNS: {fmt(self.hold_ws_ns, ' ns')} / {fmt(self.hold_tns_ns, ' ns')} "
            f"(violations: {fmt(self.hold_violations)})"
        )
        out.append(
            f"Slew/cap/fanout violations: {fmt(self.max_slew_violations)} / "
            f"{fmt(self.max_cap_violations)} / {fmt(self.max_fanout_violations)}"
        )
        out.append(f"DRC violations: {fmt(self.drc_violations)}")
        out.append(f"LVS errors: {fmt(self.lvs_errors)}")
        out.append(f"Antenna violations: {fmt(self.antenna_violations)}")
        out.append(f"Total wire length: {fmt(self.wire_length_um, ' um')}")
        if self.power_uw is not None:
            out.append(f"Power (nominal corner): {self.power_uw:,.1f} uW")
        return out


def _first(metrics: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in metrics and metrics[key] is not None:
            return metrics[key]
    # corner-suffixed keys like "timing__setup__ws__corner:nom_tt_025C_1v80"
    for key in keys:
        for k, v in metrics.items():
            if k.startswith(key + "__corner:") and v is not None:
                return v
    return None


def _min_over_corners(metrics: dict[str, Any], base: str) -> float | None:
    values = [
        n
        for k, v in metrics.items()
        if (k == base or k.startswith(base + "__corner:")) and (n := _num(v)) is not None
    ]
    return min(values) if values else None


def _max_over_corners(metrics: dict[str, Any], base: str) -> float | None:
    values = [
        n
        for k, v in metrics.items()
        if (k == base or k.startswith(base + "__corner:")) and (n := _num(v)) is not None
    ]
    return max(values) if values else None


def _int(v: Any) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def summarize_metrics(metrics: dict[str, Any]) -> MetricsSummary:
    bbox = parse_bbox(_first(metrics, "design__die__bbox"))
    core = parse_bbox(_first(metrics, "design__core__bbox"))
    width = height = area = None
    if bbox:
        width = round(bbox[2] - bbox[0], 3)
        height = round(bbox[3] - bbox[1], 3)
        area = round(width * height, 3)
    if area is None:
        area = _num(_first(metrics, "design__die__area"))
    util = _num(_first(metrics, "design__instance__utilization"))
    if util is not None and util <= 1.0:
        util = util * 100.0
    power = _first(metrics, "power__total")
    power_uw = None
    if (p := _num(power)) is not None:
        power_uw = p * 1e6  # OpenROAD reports watts
    setup_tns = _min_over_corners(metrics, "timing__setup__tns")
    hold_tns = _min_over_corners(metrics, "timing__hold__tns")
    return MetricsSummary(
        die_bbox_um=bbox,
        die_width_um=width,
        die_height_um=height,
        die_area_um2=area,
        core_bbox_um=core,
        instance_count=_int(_first(metrics, "design__instance__count")),
        stdcell_count=_int(_first(metrics, "design__instance__count__stdcell")),
        utilization_pct=round(util, 2) if util is not None else None,
        setup_ws_ns=_min_over_corners(metrics, "timing__setup__ws"),
        hold_ws_ns=_min_over_corners(metrics, "timing__hold__ws"),
        setup_tns_ns=setup_tns,
        hold_tns_ns=hold_tns,
        setup_violations=_int(_max_over_corners(metrics, "timing__setup_vio__count")),
        hold_violations=_int(_max_over_corners(metrics, "timing__hold_vio__count")),
        max_slew_violations=_int(_max_over_corners(metrics, "design__max_slew_violation__count")),
        max_cap_violations=_int(_max_over_corners(metrics, "design__max_cap_violation__count")),
        max_fanout_violations=_int(_max_over_corners(metrics, "design__max_fanout_violation__count")),
        drc_violations=_int(
            _first(metrics, "magic__drc_error__count", "klayout__drc_error__count", "route__drc_errors")
        ),
        lvs_errors=_int(_first(metrics, "design__lvs_error__count", "design__lvs_errors__count")),
        antenna_violations=_int(_first(metrics, "route__antenna_violation__count")),
        power_uw=power_uw,
        wire_length_um=_num(_first(metrics, "route__wirelength")),
        raw=metrics,
    )


def dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=False, default=str) + "\n"
