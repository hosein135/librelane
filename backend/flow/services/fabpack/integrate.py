"""Harden the shuttle *top* GDS around a completed user run.

Tiny Tapeout: LibreLane Classic into the TT tile (re-harden RTL).

ChipFoundry (Caravel): macro-first — place the hardened user GDS/LEF into
``user_project_wrapper`` (generates a LEF if the original run lacked
``Magic.WriteLEF``).

wafer.chip: LibreLane Classic on ``chip_core`` (pad ring is still wafer.space).

IHP: the user GDS *is* the submitted chip; we only rename the top cell to
the required ``FMD_QNC_*`` name.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections

from flow.models import FlowRun, FlowRunFile
from flow.services.fabpack.harness import (
    generate_caravel_wrapper,
    generate_chip_core,
    generate_tt_wrapper,
    generate_user_defines,
    ihp_gds_stem,
    tt_top_name,
)
from flow.services.fabpack.pack import (
    choose_tt_tiles,
    design_info_from_run,
    parse_top_ports,
)
from flow.services.fabpack.sources import file_source_for_run
from flow.services.fabpack.targets import canonical_pdk, get_target
from flow.services.fabpack.views import WorkTree, resolve_views
from flow.services.storage import assert_can_continue_run
from flow.services.workdir import runs_root

DATA = Path(__file__).resolve().parent / "data"

TT_TILE_SIZES: dict[str, dict[str, str]] = {
    "sky130A": {
        "1x1": "0 0 161.00 111.52",
        "1x2": "0 0 161.00 225.76",
        "2x2": "0 0 334.88 225.76",
        "3x2": "0 0 508.76 225.76",
        "4x2": "0 0 682.64 225.76",
        "6x2": "0 0 1030.40 225.76",
        "8x2": "0 0 1378.16 225.76",
    },
    "gf180mcuD": {
        "1x1": "0 0 346.64 160.72",
        "1x2": "0 0 346.64 325.36",
        "2x2": "0 0 711.20 325.36",
        "3x2": "0 0 1075.76 325.36",
        "4x2": "0 0 1440.32 325.36",
    },
    "ihp-sg13g2": {
        "1x1": "0 0 202.08 154.98",
        "1x2": "0 0 202.08 313.74",
        "2x2": "0 0 419.52 313.74",
        "3x2": "0 0 636.96 313.74",
        "4x2": "0 0 854.40 313.74",
        "6x2": "0 0 1289.28 313.74",
        "8x2": "0 0 1724.16 313.74",
    },
}

FOUNDRY_GDS_NAME = {
    "tinytapeout": "gds/{top}.gds",
    "chipfoundry-caravel": "gds/user_project_wrapper.gds",
    "wafer-space": "gds/chip_core.gds",
    "ihp-openmpw": "gds/{stem}.gds",
}


def fab_work_dir(run: FlowRun, target_id: str) -> Path:
    return runs_root() / f"user_{int(run.owner_user_id)}" / f"run_{int(run.pk)}_fab_{target_id}"


def job_state_path(run: FlowRun, target_id: str) -> Path:
    return fab_work_dir(run, target_id) / "job.json"


def foundry_gds_rel(target_id: str, *, top: str = "", stem: str = "") -> str:
    template = FOUNDRY_GDS_NAME.get(target_id, "gds/top.gds")
    return template.format(top=top or "top", stem=stem or "top")


def _write_job(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def write_job_state(run: FlowRun, target_id: str, payload: dict) -> None:
    _write_job(job_state_path(run, target_id), payload)


def _job_state_rel(target_id: str) -> str:
    return f"fabrication/{target_id}/job.json"


def _save_job_state_db(run: FlowRun, target_id: str, payload: dict) -> None:
    data = (json.dumps(payload, indent=2, default=str) + "\n").encode("utf-8")
    FlowRunFile.objects.update_or_create(
        run=run,
        relative_path=_job_state_rel(target_id),
        defaults={
            "content": data,
            "size_bytes": len(data),
            "content_type": "application/json",
        },
    )


def read_job_state(run: FlowRun, target_id: str) -> dict:
    path = job_state_path(run, target_id)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    row = FlowRunFile.objects.filter(
        run=run, relative_path=_job_state_rel(target_id)
    ).first()
    if row and row.size_bytes:
        try:
            return json.loads(bytes(row.content).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    stored = FlowRunFile.objects.filter(
        run=run, relative_path=f"fabrication/{target_id}/foundry.gds"
    ).first()
    if stored and stored.size_bytes:
        return {
            "status": "done",
            "target": target_id,
            "gds": f"fabrication/{target_id}/foundry.gds",
            "message": "Shuttle top GDS is ready.",
        }
    return {"status": "idle", "target": target_id}


def load_foundry_gds(run: FlowRun, target_id: str) -> bytes | None:
    rel = f"fabrication/{target_id}/foundry.gds"
    row = FlowRunFile.objects.filter(run=run, relative_path=rel).first()
    if row and row.size_bytes:
        return bytes(row.content)
    disk = fab_work_dir(run, target_id) / "foundry.gds"
    if disk.is_file():
        return disk.read_bytes()
    return None


LOG_TAIL_CHARS = 120_000


def job_log_path(run: FlowRun, target_id: str) -> Path:
    return fab_work_dir(run, target_id) / "job.log"


def read_job_log(run: FlowRun, target_id: str, *, max_chars: int = LOG_TAIL_CHARS) -> str:
    """Return the shuttle-build log (tail if large), preferring disk then DB."""
    path = job_log_path(run, target_id)
    text = ""
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    if not text:
        row = FlowRunFile.objects.filter(
            run=run, relative_path=f"fabrication/{target_id}/job.log"
        ).first()
        if row and row.size_bytes:
            text = bytes(row.content).decode("utf-8", errors="replace")
    if max_chars > 0 and len(text) > max_chars:
        omitted = len(text) - max_chars
        text = f"… [{omitted} characters omitted]\n\n" + text[-max_chars:]
    return text


# LibreLane rich log level, e.g. "[11:31:48] ERROR    message…"
# Require ERROR as the *level* field — not the word inside step names like
# "LVS Error Checker" or "Lint Errors Checker".
_LL_ERROR_LEVEL_RE = re.compile(
    r"^\[\d{1,2}:\d{2}:\d{2}\]\s+ERROR\b",
    re.IGNORECASE,
)


def extract_log_errors(log: str, *, limit: int = 40) -> list[str]:
    """Pull real ERROR / FlowError / Traceback lines for the UI summary."""
    if not log:
        return []
    hits: list[str] = []
    for line in log.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        # Skip informational skips / clear-checks that mention "Error" in a name.
        if upper.startswith("[") and re.match(
            r"^\[\d{1,2}:\d{2}:\d{2}\]\s+(INFO|VERBOSE|WARNING|DEBUG)\b",
            stripped,
            flags=re.I,
        ):
            continue
        if (
            _LL_ERROR_LEVEL_RE.match(stripped)
            or upper.startswith("ERROR:")
            or upper.startswith("ERROR ")
            or "FLOWERROR" in upper
            or "CRITICAL DISCONNECTED" in upper
            or stripped.startswith("Traceback ")
            or stripped.startswith("librelane.flows.flow.FlowError")
        ):
            hits.append(stripped)
            if len(hits) >= limit:
                break
    return hits


def extract_fatal_log_errors(log: str, *, limit: int = 40) -> list[str]:
    """Errors that mean the process already crashed (not deferred checkers)."""
    hits: list[str] = []
    for line in extract_log_errors(log, limit=limit * 2):
        upper = line.upper()
        # MetricChecker deferred failures are logged as ERROR mid-flow but are
        # only fatal at the end — don't treat them as live UI failures.
        if " - DEFERRED" in upper or upper.rstrip().endswith("DEFERRED"):
            continue
        if (
            "FLOWERROR" in upper
            or line.startswith("Traceback ")
            or "librelane.flows.flow.FlowError" in line
            or " FAILED WITH THE FOLLOWING" in upper
        ):
            hits.append(line)
            if len(hits) >= limit:
                break
    return hits


# LibreLane Classic rich progress, e.g.
#   Classic - Stage 59 - IR Drop Report ━━━━━╸  58/80 0:10:15
_STAGE_PROGRESS_RE = re.compile(
    r"Stage\s+(\d+)\s*-\s*(.+?)\s+[━╸─\-]+\s*(\d+)\s*/\s*(\d+)",
    re.IGNORECASE,
)
_FRAC_PROGRESS_RE = re.compile(
    r"(?:^|\s)(\d+)\s*/\s*(\d+)\s+\d+:\d{2}:\d{2}\b",
)


def parse_fab_progress(log: str, *, status: str = "") -> dict:
    """Derive a UI progress bar from the shuttle-build log."""
    status = (status or "").lower()
    if status == "done":
        return {
            "progress_pct": 100,
            "progress_step": None,
            "progress_total": None,
            "progress_label": "Shuttle top GDS ready",
            "progress_indeterminate": False,
        }
    if not log:
        if status == "running":
            return {
                "progress_pct": 2,
                "progress_step": None,
                "progress_total": None,
                "progress_label": "Starting…",
                "progress_indeterminate": True,
            }
        return {
            "progress_pct": 0,
            "progress_step": None,
            "progress_total": None,
            "progress_label": "",
            "progress_indeterminate": False,
        }

    step: int | None = None
    total: int | None = None
    stage_name = ""
    # Prefer the last Classic stage line (tail of the log).
    for match in _STAGE_PROGRESS_RE.finditer(log):
        stage_name = match.group(2).strip()
        step = int(match.group(3))
        total = int(match.group(4))
    if step is None or total is None or total <= 0:
        for match in _FRAC_PROGRESS_RE.finditer(log):
            step = int(match.group(1))
            total = int(match.group(2))
        if total is not None and total <= 0:
            total = None

    lower = log.lower()
    prep_label = ""
    prep_pct = 0
    if "starting librelane classic" in lower:
        prep_label = "LibreLane Classic starting"
        prep_pct = 8
    elif "macro-first" in lower:
        prep_label = "Placing hardened macro"
        prep_pct = 6
    elif "building abstract lef" in lower or "magic.writelef" in lower:
        prep_label = "Preparing macro LEF"
        prep_pct = 4
    elif "no lef from magic" in lower or "generating one now" in lower:
        prep_label = "Preparing views"
        prep_pct = 3

    if step is not None and total is not None and total > 0:
        # Reserve ~8% for prep; map Classic steps across the rest.
        classic_pct = 8 + int(round(92 * min(max(step, 0), total) / total))
        if stage_name:
            label = f"{stage_name} · {step}/{total}"
        else:
            label = f"Stage {step}/{total}"
        return {
            "progress_pct": min(classic_pct, 99) if status == "running" else classic_pct,
            "progress_step": step,
            "progress_total": total,
            "progress_label": label,
            "progress_indeterminate": False,
        }

    if status == "failed" or status == "stopped":
        return {
            "progress_pct": prep_pct or 0,
            "progress_step": None,
            "progress_total": None,
            "progress_label": prep_label
            or ("Build stopped" if status == "stopped" else "Build failed"),
            "progress_indeterminate": False,
        }

    if status == "running" or prep_pct:
        return {
            "progress_pct": prep_pct or 2,
            "progress_step": None,
            "progress_total": None,
            "progress_label": prep_label or "Preparing shuttle build…",
            "progress_indeterminate": True,
        }

    return {
        "progress_pct": 0,
        "progress_step": None,
        "progress_total": None,
        "progress_label": "",
        "progress_indeterminate": False,
    }


class _TeeLog:
    """StringIO that also flushes to job.log so the UI can poll mid-run."""

    def __init__(self, path: Path) -> None:
        self._buf = io.StringIO()
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("", encoding="utf-8")

    def write(self, s: str) -> int:
        self._buf.write(s)
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(s)
        except OSError:
            pass
        return len(s)

    def getvalue(self) -> str:
        return self._buf.getvalue()

    def flush(self) -> None:
        pass


def _save_foundry_gds(run: FlowRun, target_id: str, data: bytes) -> None:
    rel = f"fabrication/{target_id}/foundry.gds"
    FlowRunFile.objects.update_or_create(
        run=run,
        relative_path=rel,
        defaults={
            "content": data,
            "size_bytes": len(data),
            "content_type": "application/octet-stream",
        },
    )


def _save_job_log(run: FlowRun, target_id: str, text: str) -> None:
    data = text.encode("utf-8")
    FlowRunFile.objects.update_or_create(
        run=run,
        relative_path=f"fabrication/{target_id}/job.log",
        defaults={
            "content": data,
            "size_bytes": len(data),
            "content_type": "text/plain; charset=utf-8",
        },
    )


# Public aliases used by jobs.stop_fab_job
save_job_log = _save_job_log
save_job_state_db = _save_job_state_db


def cleanup_fab_workdir(run: FlowRun, target_id: str) -> None:
    """Remove the on-disk shuttle build tree after outputs are stored in the DB."""
    dest = fab_work_dir(run, target_id)
    if not dest.exists():
        return
    shutil.rmtree(dest, ignore_errors=True)
    # If a partial delete left an empty parent noise, ignore it.
    try:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
    except OSError:
        pass


def _materialize_sources(run: FlowRun, dest: Path) -> WorkTree:
    dest.mkdir(parents=True, exist_ok=True)
    source = file_source_for_run(run)
    info = design_info_from_run(run)
    tree = WorkTree(source, info.design_name)
    src_dir = dest / "rtl"
    src_dir.mkdir(exist_ok=True)
    for rel in tree.sources():
        (src_dir / Path(rel).name).write_bytes(tree.read_bytes(rel))
    views = resolve_views(tree)
    for kind in ("gds", "lef", "def", "netlist", "powered_netlist"):
        view = views.get(kind)
        if view:
            out = dest / "macro" / Path(view.rel_path).name
            out.parent.mkdir(exist_ok=True)
            out.write_bytes(tree.read_bytes(view.rel_path))
    return tree


def _lef_size_um(lef_path: Path) -> tuple[float, float] | None:
    text = lef_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"SIZE\s+([0-9.]+)\s+BY\s+([0-9.]+)\s*;", text, flags=re.I)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def _gds_bbox_um(gds_path: Path) -> tuple[float, float] | None:
    try:
        import klayout.db as pya  # type: ignore
    except ImportError:
        return None
    layout = pya.Layout()
    layout.read(str(gds_path))
    top = layout.top_cell()
    if top is None:
        return None
    bbox = top.bbox()
    if bbox.empty():
        return None
    dbu = layout.dbu
    return abs(bbox.right - bbox.left) * dbu, abs(bbox.top - bbox.bottom) * dbu


def _abstract_lef_from_def(def_path: Path, lef_path: Path, design_name: str) -> Path:
    """Build a BLOCK LEF with SIZE + PIN ports from a routed DEF."""
    text = def_path.read_text(encoding="utf-8", errors="replace")
    units = 1000.0
    um = re.search(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;", text, flags=re.I)
    if um:
        units = float(um.group(1))
    die = re.search(
        r"DIEAREA\s+\(\s*([-\d.]+)\s+([-\d.]+)\s*\)\s+\(\s*([-\d.]+)\s+([-\d.]+)\s*\)\s*;",
        text,
        flags=re.I,
    )
    if not die:
        raise RuntimeError(f"No DIEAREA in {def_path}")
    x0, y0, x1, y1 = (float(die.group(i)) / units for i in range(1, 5))
    width, height = abs(x1 - x0), abs(y1 - y0)

    pin_blocks: list[str] = []
    # Match each PIN statement (DEF 5.8 style).
    pin_re = re.compile(
        r"-\s+(\S+)\s+\+?\s*NET\s+\S+\s+(.*?)\s*;",
        flags=re.S | re.I,
    )
    # Simpler: scan lines after PINS
    in_pins = False
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("PINS "):
            in_pins = True
            continue
        if in_pins and stripped.upper().startswith("END PINS"):
            if current:
                pin_blocks.append("\n".join(current))
            break
        if not in_pins:
            continue
        if stripped.startswith("- "):
            if current:
                pin_blocks.append("\n".join(current))
            current = [stripped]
        elif current:
            current.append(stripped)
    if current:
        pin_blocks.append("\n".join(current))

    lef_pins: list[str] = []
    for block in pin_blocks:
        name_m = re.match(r"-\s+(\S+)", block)
        if not name_m:
            continue
        name = name_m.group(1)
        direction = "INOUT"
        dm = re.search(r"DIRECTION\s+(\w+)", block, flags=re.I)
        if dm:
            direction = dm.group(1).upper()
        use = "SIGNAL"
        umatch = re.search(r"USE\s+(\w+)", block, flags=re.I)
        if umatch:
            use = umatch.group(1).upper()
        layer = "met2"
        lm = re.search(r"LAYER\s+(\S+)", block, flags=re.I)
        if lm:
            layer = lm.group(1)
        rect = None
        rm = re.search(
            r"\(\s*([-\d.]+)\s+([-\d.]+)\s*\)\s+\(\s*([-\d.]+)\s+([-\d.]+)\s*\)",
            block,
        )
        if rm:
            rx0, ry0, rx1, ry1 = (float(rm.group(i)) / units for i in range(1, 5))
            # Convert absolute DEF coords to macro-local (origin at DIEAREA lower-left).
            rx0, rx1 = sorted((rx0 - x0, rx1 - x0))
            ry0, ry1 = sorted((ry0 - y0, ry1 - y0))
            # Clamp tiny/zero-width shapes.
            if abs(rx1 - rx0) < 0.01:
                rx1 = rx0 + 0.14
            if abs(ry1 - ry0) < 0.01:
                ry1 = ry0 + 0.14
            rect = (rx0, ry0, rx1, ry1)
        if rect is None:
            # Fallback pin stub on the bottom edge.
            rect = (0.0, 0.0, 0.14, 0.14)
        lef_pins.append(
            "\n".join(
                [
                    f"  PIN {name}",
                    f"    DIRECTION {direction} ;",
                    f"    USE {use} ;",
                    "    PORT",
                    f"      LAYER {layer} ;",
                    f"        RECT {rect[0]:.3f} {rect[1]:.3f} {rect[2]:.3f} {rect[3]:.3f} ;",
                    "    END",
                    f"  END {name}",
                ]
            )
        )

    # Ensure power pins exist for PDN hooks even if DEF omitted them.
    pin_names: set[str] = set()
    for block in lef_pins:
        m = re.match(r"  PIN (\S+)", block)
        if m:
            pin_names.add(m.group(1))
    for pname, use, layer, rect in (
        ("VPWR", "POWER", "met4", (1.0, 1.0, 2.6, height - 1.0)),
        ("VGND", "GROUND", "met4", (3.0, 1.0, 4.6, height - 1.0)),
    ):
        if pname in pin_names:
            continue
        lef_pins.append(
            "\n".join(
                [
                    f"  PIN {pname}",
                    "    DIRECTION INOUT ;",
                    f"    USE {use} ;",
                    "    PORT",
                    f"      LAYER {layer} ;",
                    f"        RECT {rect[0]:.3f} {rect[1]:.3f} {rect[2]:.3f} {rect[3]:.3f} ;",
                    "    END",
                    f"  END {pname}",
                ]
            )
        )

    body = "\n".join(
        [
            "VERSION 5.8 ;",
            'BUSBITCHARS "[]" ;',
            'DIVIDERCHAR "/" ;',
            f"MACRO {design_name}",
            "  CLASS BLOCK ;",
            f"  FOREIGN {design_name} ;",
            "  ORIGIN 0.000 0.000 ;",
            f"  SIZE {width:.3f} BY {height:.3f} ;",
            *lef_pins,
            f"END {design_name}",
            "END LIBRARY",
            "",
        ]
    )
    lef_path.write_text(body, encoding="utf-8")
    return lef_path


def _abstract_lef_from_gds(
    gds_path: Path, lef_path: Path, design_name: str, ports
) -> Path:
    size = _gds_bbox_um(gds_path)
    if size is None:
        raise RuntimeError(f"Could not read GDS bbox from {gds_path}")
    width, height = size
    pin_lines: list[str] = [
        "  PIN VPWR",
        "    DIRECTION INOUT ;",
        "    USE POWER ;",
        "    PORT",
        "      LAYER met4 ;",
        f"        RECT 1.000 1.000 2.600 {max(height - 1.0, 2.0):.3f} ;",
        "    END",
        "  END VPWR",
        "  PIN VGND",
        "    DIRECTION INOUT ;",
        "    USE GROUND ;",
        "    PORT",
        "      LAYER met4 ;",
        f"        RECT 3.000 1.000 4.600 {max(height - 1.0, 2.0):.3f} ;",
        "    END",
        "  END VGND",
    ]
    x = 6.0
    if ports is not None:
        for p in ports.ports:
            for bit in p.bit_indices():
                pname = p.name if bit is None else f"{p.name}[{bit}]"
                pin_lines.extend(
                    [
                        f"  PIN {pname}",
                        f"    DIRECTION {p.direction.upper()} ;",
                        "    USE SIGNAL ;",
                        "    PORT",
                        "      LAYER met2 ;",
                        f"        RECT {x:.3f} 0.000 {x + 0.14:.3f} 0.140 ;",
                        "    END",
                        f"  END {pname}",
                    ]
                )
                x += 0.5
                if x > width - 1:
                    x = 6.0
    body = "\n".join(
        [
            "VERSION 5.8 ;",
            'BUSBITCHARS "[]" ;',
            'DIVIDERCHAR "/" ;',
            f"MACRO {design_name}",
            "  CLASS BLOCK ;",
            f"  FOREIGN {design_name} ;",
            "  ORIGIN 0.000 0.000 ;",
            f"  SIZE {width:.3f} BY {height:.3f} ;",
            *pin_lines,
            f"END {design_name}",
            "END LIBRARY",
            "",
        ]
    )
    lef_path.write_text(body, encoding="utf-8")
    return lef_path


def _magic_write_lef(
    *,
    gds_path: Path,
    def_path: Path | None,
    out_lef: Path,
    design_name: str,
    pdk: str,
    pdk_root: str,
    log: io.TextIOBase,
) -> Path:
    """Run LibreLane Magic.WriteLEF on the hardened macro views."""
    from librelane.config import Config
    from librelane.state import DesignFormat, Path as LLPath, State
    from librelane.steps import Step

    step_dir = out_lef.parent / "magic_writelef"
    if step_dir.exists():
        shutil.rmtree(step_dir, ignore_errors=True)
    step_dir.mkdir(parents=True, exist_ok=True)

    use_gds = True
    views: dict = {DesignFormat.GDS: LLPath(str(gds_path))}
    if def_path is not None and def_path.is_file():
        views[DesignFormat.DEF] = LLPath(str(def_path))
        use_gds = False
    state = State(views)
    raw = {
        "PDK": pdk,
        "DESIGN_NAME": design_name,
        "MAGIC_LEF_WRITE_USE_GDS": use_gds,
        "MAGIC_WRITE_FULL_LEF": False,
        "MAGIC_WRITE_LEF_PINONLY": False,
        "PRIMARY_GDSII_STREAMOUT_TOOL": "klayout",
    }
    WriteLEF = Step.factory.get("Magic.WriteLEF")
    root = os_expand(pdk_root)
    # Step.start() expects a Config (has with_increment), not a bare dict.
    try:
        config = Config(raw)
    except Exception:
        config = raw
    try:
        step = WriteLEF(config, state_in=state, pdk=pdk, pdk_root=root)
    except TypeError:
        step = WriteLEF(config, state_in=state)
    with redirect_stdout(log), redirect_stderr(log):
        step.start(step_dir=str(step_dir))
    produced = list(step_dir.glob(f"{design_name}*.lef")) or list(step_dir.glob("*.lef"))
    if not produced:
        raise RuntimeError(f"Magic.WriteLEF produced no LEF under {step_dir}")
    shutil.copy2(produced[0], out_lef)
    return out_lef


def _ensure_macro_lef(
    dest: Path,
    *,
    design_name: str,
    pdk: str,
    pdk_root: str,
    ports,
    log: io.TextIOBase,
) -> Path:
    """Return a LEF for the user macro, generating one if the run lacked Magic.WriteLEF."""
    macro = dest / "macro"
    macro.mkdir(exist_ok=True)
    existing = sorted(macro.glob(f"{design_name}*.lef")) or sorted(macro.glob("*.lef"))
    if existing:
        log.write(f"Using existing macro LEF {existing[0].name}\n")
        return existing[0]

    gds_list = sorted(macro.glob(f"{design_name}*.gds")) or sorted(macro.glob("*.gds"))
    if not gds_list:
        raise RuntimeError(
            "ChipFoundry macro insertion needs the hardened GDS from KLayout.StreamOut."
        )
    gds_path = gds_list[0]
    def_list = sorted(macro.glob(f"{design_name}*.def")) or sorted(macro.glob("*.def"))
    def_path = def_list[0] if def_list else None
    out_lef = macro / f"{design_name}.lef"

    try:
        log.write(
            "No LEF from Magic.WriteLEF in the original run — generating one now…\n"
        )
        return _magic_write_lef(
            gds_path=gds_path,
            def_path=def_path,
            out_lef=out_lef,
            design_name=design_name,
            pdk=pdk,
            pdk_root=pdk_root,
            log=log,
        )
    except Exception as exc:
        log.write(f"Magic.WriteLEF failed ({exc}); falling back to abstract LEF.\n")

    if def_path is not None:
        log.write(f"Building abstract LEF from DEF {def_path.name}\n")
        return _abstract_lef_from_def(def_path, out_lef, design_name)

    log.write(f"Building abstract LEF from GDS bbox of {gds_path.name}\n")
    return _abstract_lef_from_gds(gds_path, out_lef, design_name, ports)


def _adapt_def(src: Path, dest: Path, design_name: str) -> None:
    text = src.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"^DESIGN\s+\S+\s*;", f"DESIGN {design_name} ;", text, count=1, flags=re.M)
    dest.write_text(text, encoding="utf-8")


def _parse_die_area(spec: str) -> list[float]:
    parts = [float(x) for x in spec.split()]
    if len(parts) != 4:
        raise ValueError(f"Bad DIE_AREA {spec!r}")
    return parts


def _classic_config_base(pdk: str, clock_period: float) -> dict:
    return {
        "PDK": pdk,
        "CLOCK_PERIOD": clock_period,
        "PRIMARY_GDSII_STREAMOUT_TOOL": "klayout",
        "RUN_LINTER": False,
        "QUIT_ON_SYNTH_CHECKS": False,
        "QUIT_ON_MAGIC_DRC": False,
        "QUIT_ON_LVS_ERROR": False,
        "QUIT_ON_TR_DRC": False,
        "ERROR_ON_TR_DRC": False,
        # Shuttle harnesses leave many Caravel/TT pins unused; still emit GDS.
        "ERROR_ON_DISCONNECTED_PINS": False,
        "QUIT_ON_DISCONNECTED_PINS": False,
        "ERROR_ON_LONG_WIRE": False,
        # Macro-first wrappers use assign-tieoffs for unused padframe pins.
        "ERROR_ON_NL_ASSIGN_STATEMENTS": False,
        # Partial PDN connectivity is common for wrapper+macro packaging;
        # LibreLane itself notes these can be ignored when LVS is not required.
        "ERROR_ON_PDN_VIOLATIONS": False,
        "ERROR_ON_ILLEGAL_OVERLAPS": False,
        # Shuttle packaging prioritizes emitting a top GDS over signoff reports.
        "RUN_IRDROP_REPORT": False,
        "RUN_LVS": False,
        "RUN_KLAYOUT_XOR": False,
        "RUN_MAGIC_DRC": False,
        "RUN_KLAYOUT_DRC": False,
        "PL_TARGET_DENSITY_PCT": 55,
        "GRT_ALLOW_CONGESTION": True,
        "MAGIC_DEF_LABELS": False,
    }


def _run_classic(config: dict, design_dir: Path, pdk: str, pdk_root: str, log: io.TextIOBase):
    from librelane.flows import Flow
    from librelane.flows.flow import FlowError

    Classic = Flow.factory.get("Classic")
    root = os_expand(pdk_root)
    try:
        flow = Classic(
            config,
            design_dir=str(design_dir),
            pdk=pdk,
            pdk_root=root,
        )
    except TypeError:
        flow = Classic(config, design_dir=str(design_dir))
    try:
        with redirect_stdout(log), redirect_stderr(log):
            state = flow.start()
        return state, flow
    except FlowError as exc:
        # LibreLane saves final/ (including GDS) before raising deferred metric
        # errors. For shuttle packaging, keep going if stream-out produced a GDS.
        msg = str(exc)
        if "deferred errors" not in msg.lower():
            raise
        design_name = str(config.get("DESIGN_NAME") or "")
        try:
            gds = _find_gds(design_dir, design_name)
        except RuntimeError:
            raise
        log.write(
            "\nLibreLane reported deferred checker errors after stream-out; "
            f"continuing with produced GDS ({gds.name}).\n"
            f"Deferred detail: {msg}\n"
        )
        return None, flow


def os_expand(path: str) -> str:
    from os.path import expanduser

    return expanduser(path or "") or expanduser("~/.ciel")


def _find_gds(design_dir: Path, design_name: str) -> Path:
    candidates: list[Path] = []
    for pattern in (
        f"runs/**/final/gds/{design_name}*.gds",
        "runs/**/final/gds/*.gds",
        "runs/**/*klayout*/*.gds",
        "**/*.klayout.gds",
        f"**/{design_name}.gds",
    ):
        candidates.extend(design_dir.glob(pattern))
    files = [p for p in candidates if p.is_file()]
    if not files:
        raise RuntimeError(
            f"LibreLane finished but no GDS was found under {design_dir}. "
            "Check job.log for DRC/PnR errors."
        )
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0]


def _rename_gds_top(src: Path, dest: Path, new_name: str) -> None:
    try:
        import klayout.db as pya  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "KLayout Python module is required to rename the IHP GDS top cell."
        ) from exc
    layout = pya.Layout()
    layout.read(str(src))
    top = layout.top_cell()
    if top is None:
        raise RuntimeError("GDS has no top cell.")
    top.name = new_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    layout.write(str(dest))


def _dir_verilog(design_dir: Path, *names: str) -> list[str]:
    src = design_dir / "src"
    skip = {"user_defines.v", "user_macro.bb.v"}
    if names:
        found = [f"dir::src/{name}" for name in names if (src / name).is_file()]
        if found:
            return found
    return [
        f"dir::src/{p.name}"
        for p in sorted(src.iterdir())
        if p.is_file() and p.suffix in {".v", ".sv"} and p.name not in skip
    ]


def _blackbox_module(module: str, ports, *, with_power: bool = True) -> str:
    decls: list[str] = []
    names: list[str] = []
    if with_power:
        names.extend(["VPWR", "VGND"])
        decls.append("    inout VPWR;")
        decls.append("    inout VGND;")
    if ports is not None and getattr(ports, "ports", None):
        for p in ports.ports:
            names.append(p.name)
            decls.append(f"    {p.direction} {p.range_decl}{p.name};")
    if not names:
        return f"(* blackbox *)\nmodule {module};\nendmodule\n"
    port_list = ", ".join(names)
    body = "\n".join(decls)
    return f"(* blackbox *)\nmodule {module} ({port_list});\n{body}\nendmodule\n"


def _reset_work_tree(dest: Path) -> None:
    """Clear previous integration outputs but keep job.json / job.log."""
    dest.mkdir(parents=True, exist_ok=True)
    keep = {"job.json", "job.log"}
    for child in dest.iterdir():
        if child.name in keep:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


def integrate_shuttle_top(run: FlowRun, target_id: str, log: io.TextIOBase) -> Path:
    """Build the shuttle-top GDS. Returns the path to foundry.gds."""
    assert_can_continue_run(run)
    target = get_target(target_id, run.pdk)
    pdk = canonical_pdk(run.pdk)
    dest = fab_work_dir(run, target.id)
    _reset_work_tree(dest)
    log.write(f"Shuttle: {target.label} ({target.id})  PDK={pdk}\n")

    info = design_info_from_run(run)
    tree = _materialize_sources(run, dest)
    views = resolve_views(tree)
    ports = parse_top_ports(tree, info, views)
    pdk_root = run.pdk_root or getattr(settings, "PDK_ROOT", "~/.ciel")

    if target.id == "ihp-openmpw":
        gds_view = views.get("gds")
        if gds_view is None:
            raise RuntimeError("No GDS from KLayout.StreamOut — cannot build an IHP package.")
        src = dest / "macro" / Path(gds_view.rel_path).name
        out = dest / "foundry.gds"
        log.write(f"Renaming GDS top cell to {ihp_gds_stem(info.design_name)}\n")
        _rename_gds_top(src, out, ihp_gds_stem(info.design_name))
        return out

    design_dir = dest / "ll"
    design_dir.mkdir(exist_ok=True)
    (design_dir / "src").mkdir(exist_ok=True)

    if target.id == "tinytapeout":
        top, wrapper, _mapping = generate_tt_wrapper(info, ports)
        for src in (dest / "rtl").iterdir():
            if src.is_file():
                shutil.copy2(src, design_dir / "src" / src.name)
        if wrapper:
            (design_dir / "src" / f"{top}.v").write_text(wrapper, encoding="utf-8")
        tiles, _ = choose_tt_tiles(info)
        sizes = TT_TILE_SIZES.get(pdk) or TT_TILE_SIZES["sky130A"]
        if tiles not in sizes:
            tiles = "1x1"
        die = sizes[tiles]
        def_src = DATA / "def" / f"tt_{pdk}_1x1.def"
        config = _classic_config_base(pdk, float(info.clock_period_ns or 20))
        config.update(
            {
                "DESIGN_NAME": top,
                "VERILOG_FILES": _dir_verilog(design_dir),
                "CLOCK_PORT": "clk",
                "CLOCK_NET": "clk",
                "FP_SIZING": "absolute",
                "DIE_AREA": _parse_die_area(die),
                "VDD_PIN": "VPWR",
                "GND_PIN": "VGND",
                "FP_PDN_MULTILAYER": False,
            }
        )
        if pdk == "sky130A":
            config["RT_MAX_LAYER"] = "met4"
        pin_cfg = DATA / "tt_pin_order.cfg"
        if pin_cfg.is_file():
            shutil.copy2(pin_cfg, design_dir / "pin_order.cfg")
            config["FP_PIN_ORDER_CFG"] = "dir::pin_order.cfg"
            config["IO_PIN_ORDER_CFG"] = "dir::pin_order.cfg"
        if def_src.is_file() and tiles == "1x1":
            _adapt_def(def_src, design_dir / "template.def", top)
            config["FP_DEF_TEMPLATE"] = "dir::template.def"
            log.write(f"Using Tiny Tapeout {tiles} DEF template ({die} µm)\n")
        else:
            log.write(f"Using Tiny Tapeout die {tiles} ({die} µm) without a DEF template\n")
        foundry_name = foundry_gds_rel("tinytapeout", top=top)

    elif target.id == "chipfoundry-caravel":
        # Option 1 — Macro-First: place the hardened user GDS/LEF into the
        # golden wrapper instead of flattening RTL across the whole die.
        wrapper, _mapping = generate_caravel_wrapper(info, ports)
        (design_dir / "src" / "user_defines.v").write_text(
            generate_user_defines(_mapping), encoding="utf-8"
        )
        if not wrapper:
            raise RuntimeError(
                "Top module already looks like user_project_wrapper; "
                "macro-first insertion expects a user macro, not the wrapper itself."
            )
        (design_dir / "src" / "user_project_wrapper.v").write_text(
            wrapper, encoding="utf-8"
        )

        gds_list = sorted((dest / "macro").glob("*.gds"))
        if not gds_list:
            raise RuntimeError(
                "ChipFoundry macro-first hardening requires the hardened GDS "
                "(KLayout.StreamOut) from the completed run."
            )
        lef_path = _ensure_macro_lef(
            dest,
            design_name=info.design_name,
            pdk=pdk,
            pdk_root=pdk_root,
            ports=ports,
            log=log,
        )
        gds_path = gds_list[0]
        size = _lef_size_um(lef_path) or _gds_bbox_um(gds_path) or (500.0, 500.0)
        # Center the macro in the 2920×3520 µm user area with a small margin.
        place_x = max(50.0, (2920.0 - size[0]) / 2.0)
        place_y = max(50.0, (3520.0 - size[1]) / 2.0)

        (design_dir / "src" / "user_macro.bb.v").write_text(
            _blackbox_module(info.design_name, ports), encoding="utf-8"
        )

        # Keep MACROS to GDS/LEF + placement only. Feeding nl/pnl here makes
        # Yosys load the gate netlist then replace it with the blackbox, and is
        # unnecessary for elaborate-only macro-first hardening.
        macro_entry: dict = {
            "gds": [str(gds_path)],
            "lef": [str(lef_path)],
            "instances": {
                "user": {
                    "location": [round(place_x, 3), round(place_y, 3)],
                    "orientation": "N",
                }
            },
        }

        config = _classic_config_base(pdk, max(float(info.clock_period_ns or 25), 25))
        config.update(
            {
                "DESIGN_NAME": "user_project_wrapper",
                "VERILOG_FILES": ["dir::src/user_project_wrapper.v"],
                "EXTRA_VERILOG_MODELS": ["dir::src/user_macro.bb.v"],
                "VERILOG_FILES_BLACKBOX": ["dir::src/user_macro.bb.v"],
                "EXTRA_GDS_FILES": [str(gds_path)],
                "EXTRA_LEFS": [str(lef_path)],
                "MACROS": {info.design_name: macro_entry},
                "SYNTH_ELABORATE_ONLY": True,
                "RUN_CTS": False,
                "RUN_FILL_INSERTION": False,
                "RUN_TAP_ENDCAP_INSERTION": False,
                "RUN_POST_GPL_DESIGN_REPAIR": False,
                "RUN_POST_CTS_RESIZER_TIMING": False,
                "CLOCK_PORT": "wb_clk_i",
                "CLOCK_NET": "wb_clk_i",
                "FP_SIZING": "absolute",
                "DIE_AREA": [0, 0, 2920, 3520],
                "VDD_NETS": ["vccd1", "vccd2", "vdda1", "vdda2"],
                "GND_NETS": ["vssd1", "vssd2", "vssa1", "vssa2"],
                "PDN_CORE_RING": True,
                "MAGIC_ZEROIZE_ORIGIN": False,
                # Format: <inst> <vdd_net> <gnd_net> <vdd_pin> <gnd_pin>
                # Pin names must match the macro LEF (VPWR/VGND), not the nets.
                "PDN_MACRO_CONNECTIONS": ["user vccd1 vssd1 VPWR VGND"],
                "PDN_CONNECT_MACROS_TO_GRID": True,
            }
        )
        pin_cfg = DATA / "caravel_pin_order.cfg"
        if pin_cfg.is_file():
            shutil.copy2(pin_cfg, design_dir / "pin_order.cfg")
            config["FP_PIN_ORDER_CFG"] = "dir::pin_order.cfg"
            config["IO_PIN_ORDER_CFG"] = "dir::pin_order.cfg"
        def_src = DATA / "def" / "user_project_wrapper.def"
        if def_src.is_file():
            shutil.copy2(def_src, design_dir / "user_project_wrapper.def")
            config["FP_DEF_TEMPLATE"] = "dir::user_project_wrapper.def"
            log.write("Using Caravel golden user_project_wrapper.def (2920 x 3520 µm)\n")
        log.write(
            f"Macro-first: inserting {gds_path.name} + {lef_path.name} "
            f"as instance 'user' at ({place_x:.1f}, {place_y:.1f}) µm "
            f"(size {size[0]:.1f}×{size[1]:.1f} µm)\n"
        )
        foundry_name = foundry_gds_rel("chipfoundry-caravel")

    elif target.id == "wafer-space":
        core, _mapping = generate_chip_core(info, ports)
        core_path = design_dir / "src" / "chip_core.sv"
        core_path.write_text(core, encoding="utf-8")
        for src in (dest / "rtl").iterdir():
            if src.is_file():
                shutil.copy2(src, design_dir / "src" / src.name)
        config = _classic_config_base(pdk, float(info.clock_period_ns or 20))
        config.update(
            {
                "DESIGN_NAME": "chip_core",
                "VERILOG_FILES": _dir_verilog(design_dir),
                "CLOCK_PORT": "clk",
                "CLOCK_NET": "clk",
                "FP_SIZING": "absolute",
                "DIE_AREA": _parse_die_area(TT_TILE_SIZES["gf180mcuD"]["1x1"]),
                "USE_SLANG": True,
            }
        )
        log.write(
            "Hardening chip_core (user logic). wafer.space still wraps this in chip_top pad ring.\n"
        )
        foundry_name = "gds/chip_core.gds"

    else:
        raise ValueError(f"No integration flow for {target.id}")

    (design_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    log.write("Starting LibreLane Classic (shuttle top). This can take 30–90 minutes.\n")
    _run_classic(config, design_dir, pdk, pdk_root, log)
    gds_path = _find_gds(design_dir, str(config["DESIGN_NAME"]))
    out = dest / "foundry.gds"
    shutil.copy2(gds_path, out)
    (dest / "foundry_name.txt").write_text(foundry_name, encoding="utf-8")
    log.write(f"Shuttle top GDS: {gds_path} -> {out}\n")
    return out


def run_integration_job(run_id: int, target_id: str) -> None:
    run = FlowRun.objects.get(pk=run_id)
    dest = fab_work_dir(run, target_id)
    dest.mkdir(parents=True, exist_ok=True)
    state_file = job_state_path(run, target_id)
    log = _TeeLog(dest / "job.log")
    _write_job(
        state_file,
        {
            "status": "running",
            "target": target_id,
            "message": "Building shuttle top GDS (LibreLane chip-level flow)…",
        },
    )
    try:
        close_old_connections()
        gds_path = integrate_shuttle_top(run, target_id, log)
        data = gds_path.read_bytes()
        text = log.getvalue()
        close_old_connections()
        _save_foundry_gds(run, target_id, data)
        _save_job_log(run, target_id, text)
        done = {
            "status": "done",
            "target": target_id,
            "gds": f"fabrication/{target_id}/foundry.gds",
            "bytes": len(data),
            "message": "Shuttle top GDS is ready.",
            "pid": None,
        }
        _save_job_state_db(run, target_id, done)
        cleanup_fab_workdir(run, target_id)
    except Exception as exc:
        text = log.getvalue() + "\n" + traceback.format_exc()
        failed = {
            "status": "failed",
            "target": target_id,
            "error": str(exc),
            "message": str(exc),
            "pid": None,
        }
        try:
            close_old_connections()
            _save_job_log(run, target_id, text)
            _save_job_state_db(run, target_id, failed)
        except Exception:
            pass
        cleanup_fab_workdir(run, target_id)
        raise
