"""Assemble foundry-ready zip packages for each shuttle target."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from flow.services.fabpack.harness import (
    PortMap,
    generate_caravel_wrapper,
    generate_chip_core,
    generate_tt_wrapper,
    generate_user_defines,
    ident_slug,
    ihp_gds_stem,
)
from flow.services.fabpack.targets import FabTarget
from flow.services.fabpack.verilog_ports import ModulePorts, parse_module_ports
from flow.services.fabpack.views import (
    DesignInfo,
    StepRecord,
    ViewSet,
    WorkTree,
    dump_json,
    merged_metrics,
    resolve_views,
    summarize_metrics,
)

LICENSE_PATH = Path(__file__).resolve().parent / "data" / "LICENSE-Apache-2.0.txt"

# Tiny Tapeout tile sizes (µm), smallest first. SKY130 values from TT docs;
# other PDKs use the same progression so a fitting tile can still be suggested.
_TT_TILES: list[tuple[str, float, float]] = [
    ("1x1", 167.0, 108.0),
    ("1x2", 167.0, 225.0),
    ("2x2", 344.0, 225.0),
    ("3x2", 520.0, 225.0),
    ("4x2", 696.0, 225.0),
    ("6x2", 1048.0, 225.0),
    ("8x2", 1400.0, 225.0),
]


def design_info_from_run(run: object) -> DesignInfo:
    try:
        from flow.services.setup import librelane_version

        version = librelane_version()
    except Exception:
        version = "unknown"
    author = ""
    try:
        author = run.owner_user.username
    except Exception:
        pass
    created = run.created_at.isoformat() if run.created_at else ""
    steps = [
        StepRecord(
            order=s.order,
            step_id=s.step_id,
            title=s.title,
            status=s.status,
            output=s.output or {},
        )
        for s in run.steps.all()
    ]
    return DesignInfo(
        design_name=run.design_name or "design",
        pdk=run.pdk,
        pdk_family=run.pdk_family,
        clock_period_ns=float(run.clock_period or 10),
        clock_port="clk",
        run_id=int(run.pk),
        run_name=run.name or run.design_name or "design",
        librelane_version=version,
        created_at=created,
        steps=steps,
        author=author,
        project_name=run.name or run.design_name or "design",
        project_description=(
            f"Hardened {run.design_name} on {run.pdk} with LibreLane Web."
        ),
    )


def parse_top_ports(tree: WorkTree, info: DesignInfo, views: ViewSet) -> ModulePorts | None:
    candidates: list[str] = []
    for kind in ("netlist", "powered_netlist", "synth_netlist"):
        view = views.get(kind)
        if view:
            candidates.append(view.rel_path)
    candidates.extend(tree.sources())
    seen: set[str] = set()
    for rel in candidates:
        if rel in seen or not tree.exists(rel):
            continue
        seen.add(rel)
        try:
            text = tree.read_text(rel)
        except Exception:
            continue
        parsed = parse_module_ports(text, info.design_name)
        if parsed and parsed.ports:
            return parsed
    return None


def _add_hardened(tree: WorkTree, views: ViewSet, files: dict[str, bytes], prefix: str) -> None:
    for view in views.views.values():
        dest = f"{prefix}/{view.kind}/{Path(view.rel_path).name}"
        files[dest] = tree.read_bytes(view.rel_path)
    for kind, group in views.multi.items():
        for view in group:
            dest = f"{prefix}/{kind}/{Path(view.rel_path).name}"
            files[dest] = tree.read_bytes(view.rel_path)


def _add_sources(tree: WorkTree, files: dict[str, bytes], dest_dir: str) -> list[str]:
    names: list[str] = []
    for rel in tree.sources():
        name = Path(rel).name
        files[f"{dest_dir}/{name}"] = tree.read_bytes(rel)
        names.append(name)
    return names


def _license_text() -> bytes:
    if LICENSE_PATH.is_file():
        return LICENSE_PATH.read_bytes()
    return b"Apache License 2.0\n"


def _yaml_escape(value: str) -> str:
    text = value.replace("\n", " ").strip()
    if any(ch in text for ch in ":#{}[]&*?|>!%@`'\"\\"):
        return json.dumps(text)
    return text or '""'


def _pinout_yaml(mapping: PortMap) -> str:
    labels = {a.harness: a.design for a in mapping.assignments}
    out: list[str] = ["pinout:", "  # Inputs"]
    for i in range(8):
        out.append(f"  ui[{i}]: {_yaml_escape(labels.get(f'ui_in[{i}]', ''))}")
    out.append("  # Outputs")
    for i in range(8):
        out.append(f"  uo[{i}]: {_yaml_escape(labels.get(f'uo_out[{i}]', ''))}")
    out.append("  # Bidirectional pins")
    for i in range(8):
        design = labels.get(f"uio_in[{i}]", labels.get(f"uio[{i}]", ""))
        out.append(f"  uio[{i}]: {_yaml_escape(design)}")
    return "\n".join(out)


def choose_tt_tiles(info: DesignInfo) -> tuple[str, list[str]]:
    warnings: list[str] = []
    metrics = summarize_metrics(merged_metrics(info))
    width = metrics.die_width_um
    height = metrics.die_height_um
    if width is None or height is None:
        return "1x1", warnings
    for name, tw, th in _TT_TILES:
        if width <= tw + 1.0 and height <= th + 1.0:
            return name, warnings
    last = _TT_TILES[-1][0]
    warnings.append(
        f"Die {width:g} x {height:g} um exceeds the largest Tiny Tapeout tile "
        f"({_TT_TILES[-1][1]:g} x {_TT_TILES[-1][2]:g} um). Using {last}; "
        "the shuttle will re-harden from RTL into the chosen tile."
    )
    return last, warnings


def _pinout_markdown(mapping: PortMap) -> str:
    rows = mapping.pinout_rows()
    lines = ["# Pin map", "", "| Harness pin | Design port |", "| --- | --- |"]
    if not rows:
        lines.append("| (none) | (clock/reset only, or ports could not be parsed) |")
    for harness, design in rows:
        lines.append(f"| `{harness}` | `{design}` |")
    if mapping.warnings:
        lines.extend(["", "## Warnings", ""])
        for warning in mapping.warnings:
            lines.append(f"- {warning}")
    lines.append("")
    return "\n".join(lines)


def _metrics_text(info: DesignInfo) -> str:
    summary = summarize_metrics(merged_metrics(info))
    return "Sign-off summary\n" + "\n".join(summary.lines()) + "\n"


def _tt_info_yaml(
    info: DesignInfo,
    top: str,
    source_files: list[str],
    tiles: str,
    mapping: PortMap,
) -> str:
    sources = "\n".join(f"    - {_yaml_escape(name)}" for name in source_files) or '    - "project.v"'
    return f"""# Tiny Tapeout project information
project:
  title: {_yaml_escape(info.project_name)}
  author: {_yaml_escape(info.author or "LibreLane Web user")}
  discord: ""
  description: {_yaml_escape(info.project_description)}
  language: {_yaml_escape(info.top_language)}
  clock_hz: {info.clock_hz}
  tiles: "{tiles}"
  top_module: "{top}"
  source_files:
{sources}

{_pinout_yaml(mapping)}

yaml_version: 6
"""


def _tt_test_py(top: str) -> str:
    return f'''import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles


@cocotb.test()
async def test_project(dut):
    dut._log.info("Start")
    clock = Clock(dut.clk, 10, units="us")
    cocotb.start_soon(clock.start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 10)
    dut._log.info("{top} reset released")
'''


def _tt_tb_v(top: str) -> str:
    return f"""`default_nettype none
`timescale 1ns / 1ps

module tb ();
    initial begin
        $dumpfile("tb.vcd");
        $dumpvars(0, tb);
        #1;
    end

    reg clk;
    reg rst_n;
    reg ena;
    reg [7:0] ui_in;
    reg [7:0] uio_in;
    wire [7:0] uo_out;
    wire [7:0] uio_out;
    wire [7:0] uio_oe;
`ifdef GL_TEST
    wire VPWR = 1'b1;
    wire VGND = 1'b0;
`endif

    {top} user_project (
`ifdef GL_TEST
        .VPWR(VPWR),
        .VGND(VGND),
`endif
        .ui_in (ui_in),
        .uo_out (uo_out),
        .uio_in (uio_in),
        .uio_out(uio_out),
        .uio_oe (uio_oe),
        .ena (ena),
        .clk (clk),
        .rst_n (rst_n)
    );
endmodule
"""


def _tt_makefile(top: str) -> str:
    return f"""SIM ?= icarus
TOPLEVEL_LANG ?= verilog
VERILOG_SOURCES += $(PWD)/tb.v $(PWD)/../src/{top}.v
TOPLEVEL = tb
MODULE = test
include $(shell cocotb-config --makefiles)/Makefile.sim
"""


def pack_tinytapeout(
    info: DesignInfo, tree: WorkTree, views: ViewSet, ports: ModulePorts | None
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    top, wrapper, mapping = generate_tt_wrapper(info, ports)
    source_names = _add_sources(tree, files, "src")
    if wrapper:
        wrapper_name = f"{top}.v"
        files[f"src/{wrapper_name}"] = wrapper.encode("utf-8")
        if wrapper_name not in source_names:
            source_names.insert(0, wrapper_name)
    tiles, tile_warnings = choose_tt_tiles(info)
    mapping.warnings.extend(tile_warnings)
    files["info.yaml"] = _tt_info_yaml(info, top, source_names, tiles, mapping).encode("utf-8")
    files["docs/info.md"] = (
        f"## How it works\n\n{info.project_description}\n\n"
        "## How to test\n\nApply clk / rst_n and the mapped inputs listed in PINOUT.md.\n\n"
        "## External hardware\n\nNone specified.\n"
    ).encode("utf-8")
    files["PINOUT.md"] = _pinout_markdown(mapping).encode("utf-8")
    files["test/tb.v"] = _tt_tb_v(top).encode("utf-8")
    files["test/test.py"] = _tt_test_py(top).encode("utf-8")
    files["test/Makefile"] = _tt_makefile(top).encode("utf-8")
    files["src/config.json"] = json.dumps(
        {
            "DESIGN_NAME": top,
            "VERILOG_FILES": [f"dir::{name}" for name in source_names],
            "CLOCK_PERIOD": info.clock_period_ns,
            "CLOCK_PORT": "clk",
            "FP_SIZING": "absolute",
        },
        indent=2,
    ).encode("utf-8") + b"\n"
    _add_hardened(tree, views, files, "hardened")
    files["LICENSE"] = _license_text()
    files["README.md"] = _readme_tt(info, top, mapping).encode("utf-8")
    files["SIGNOFF.txt"] = _metrics_text(info).encode("utf-8")
    return files


def pack_caravel(
    info: DesignInfo, tree: WorkTree, views: ViewSet, ports: ModulePorts | None
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    wrapper, mapping = generate_caravel_wrapper(info, ports)
    _add_sources(tree, files, "verilog/rtl")
    if wrapper:
        files["verilog/rtl/user_project_wrapper.v"] = wrapper.encode("utf-8")
    files["verilog/rtl/user_defines.v"] = generate_user_defines(mapping).encode("utf-8")
    gds = views.get("gds")
    if gds:
        files[f"gds/{info.design_name}.gds"] = tree.read_bytes(gds.rel_path)
        files[f"openlane/{info.design_name}/{info.design_name}.gds"] = tree.read_bytes(gds.rel_path)
    lef = views.get("lef")
    if lef:
        files[f"lef/{info.design_name}.lef"] = tree.read_bytes(lef.rel_path)
    net = views.get("netlist") or views.get("powered_netlist")
    if net:
        files[f"verilog/gl/{info.design_name}.v"] = tree.read_bytes(net.rel_path)
    _add_hardened(tree, views, files, "hardened")
    files["librelane/user_project_wrapper/config.json"] = json.dumps(
        {
            "DESIGN_NAME": "user_project_wrapper",
            "VERILOG_FILES": ["dir::../../verilog/rtl/user_project_wrapper.v"],
            "VERILOG_FILES_BLACKBOX": [f"dir::../../verilog/gl/{info.design_name}.v"],
            "EXTRA_LEFS": [f"dir::../../lef/{info.design_name}.lef"],
            "EXTRA_GDS_FILES": [f"dir::../../gds/{info.design_name}.gds"],
            "CLOCK_PORT": "wb_clk_i",
            "CLOCK_NET": "wb_clk_i",
            "CLOCK_PERIOD": max(info.clock_period_ns, 25),
            "FP_SIZING": "absolute",
            "DIE_AREA": [0, 0, 2920, 3520],
        },
        indent=2,
    ).encode("utf-8") + b"\n"
    files[".cf/project.json"] = json.dumps(
        {
            "name": info.project_name,
            "type": "digital",
            "submission_state": "Draft",
            "pdk": info.pdk,
            "design": info.design_name,
        },
        indent=2,
    ).encode("utf-8") + b"\n"
    files["PINOUT.md"] = _pinout_markdown(mapping).encode("utf-8")
    files["LICENSE"] = _license_text()
    files["README.md"] = _readme_caravel(info, mapping).encode("utf-8")
    files["SIGNOFF.txt"] = _metrics_text(info).encode("utf-8")
    return files


def pack_wafer(
    info: DesignInfo, tree: WorkTree, views: ViewSet, ports: ModulePorts | None
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    core, mapping = generate_chip_core(info, ports)
    _add_sources(tree, files, "src")
    files["src/chip_core.sv"] = core.encode("utf-8")
    gds = views.get("gds")
    if gds:
        files["gds/chip_core.gds"] = tree.read_bytes(gds.rel_path)
        files[f"gds/{info.design_name}.gds"] = tree.read_bytes(gds.rel_path)
    lef = views.get("lef")
    if lef:
        files[f"lef/{info.design_name}.lef"] = tree.read_bytes(lef.rel_path)
    _add_hardened(tree, views, files, "hardened")
    files["PINOUT.md"] = _pinout_markdown(mapping).encode("utf-8")
    files["LICENSE"] = _license_text()
    files["README.md"] = _readme_wafer(info, mapping).encode("utf-8")
    files["SIGNOFF.txt"] = _metrics_text(info).encode("utf-8")
    return files


def pack_ihp(
    info: DesignInfo, tree: WorkTree, views: ViewSet, ports: ModulePorts | None
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    slug = ident_slug(info.design_name)
    root = slug
    gds_stem = ihp_gds_stem(info.design_name)
    gds = views.get("gds")
    if gds:
        files[f"{root}/design_data/gds/{gds_stem}.gds"] = tree.read_bytes(gds.rel_path)
    lef = views.get("lef")
    if lef:
        files[f"{root}/design_data/lef/{info.design_name}.lef"] = tree.read_bytes(lef.rel_path)
    net = views.get("netlist") or views.get("powered_netlist")
    if net:
        files[f"{root}/design_data/verilog/{info.design_name}.v"] = tree.read_bytes(net.rel_path)
    sdc = views.get("sdc")
    if sdc:
        files[f"{root}/design_data/sdc/{info.design_name}.sdc"] = tree.read_bytes(sdc.rel_path)
    for view in views.multi.get("lib") or []:
        files[f"{root}/design_data/lib/{Path(view.rel_path).name}"] = tree.read_bytes(view.rel_path)
    for view in views.multi.get("spef") or []:
        files[f"{root}/design_data/spef/{Path(view.rel_path).name}"] = tree.read_bytes(view.rel_path)
    for kind, dest in (
        ("drc_reports", "val/drc"),
        ("lvs_reports", "val/lvs"),
        ("sta_reports", "val/reports"),
        ("logs", "val/log"),
    ):
        for view in views.multi.get(kind) or []:
            files[f"{root}/{dest}/{Path(view.rel_path).name}"] = tree.read_bytes(view.rel_path)
    _add_sources(tree, files, f"{root}/design_data/verilog/rtl")
    def_view = views.get("def")
    if def_view:
        files[f"{root}/design_data/def/{info.design_name}.def"] = tree.read_bytes(def_view.rel_path)
    spice = views.get("spice")
    if spice:
        files[f"{root}/design_data/spice/{info.design_name}.spice"] = tree.read_bytes(spice.rel_path)

    metrics = summarize_metrics(merged_metrics(info))
    metadata = {
        "ProjectName": info.project_name,
        "DesignName": info.design_name,
        "FinalGDSName": f"{gds_stem}.gds",
        "PDK": info.pdk,
        "Language": info.top_language,
        "Type": "digital",
        "Author": info.author,
        "Description": info.project_description,
        "Tool": f"{info.tool_name} / LibreLane {info.librelane_version}",
        "ClockPeriod_ns": info.clock_period_ns,
        "DieWidth_um": metrics.die_width_um,
        "DieHeight_um": metrics.die_height_um,
        "Contact": info.author,
    }
    files[f"{root}/metadata.json"] = dump_json(metadata).encode("utf-8")
    files[f"{root}/doc/README.md"] = (
        f"# {info.project_name}\n\n{info.project_description}\n\n"
        f"PDK: {info.pdk}. Clock period: {info.clock_period_ns} ns.\n\n"
        f"Final GDS: `design_data/gds/{gds_stem}.gds` (FMD_QNC prefix required by IHP).\n"
    ).encode("utf-8")
    files[f"{root}/val/README.md"] = (
        "Sign-off reports copied from the LibreLane run (DRC, LVS, STA, logs).\n"
    ).encode("utf-8")
    files[f"{root}/SIGNOFF.txt"] = _metrics_text(info).encode("utf-8")
    files["README.md"] = _readme_ihp(info, gds_stem).encode("utf-8")
    files["LICENSE"] = _license_text()
    if ports:
        mapping = PortMap()
        mapping.warnings.append("IHP Open MPW uses the native design pinout (no shuttle harness).")
        files[f"{root}/doc/ports.md"] = (
            "# Ports\n\n"
            + "\n".join(
                f"- `{p.direction} {p.range_decl}{p.name}`" for p in ports.ports
            )
            + "\n"
        ).encode("utf-8")
    return files


def _readme_tt(info: DesignInfo, top: str, mapping: PortMap) -> str:
    warns = "\n".join(f"- {w}" for w in mapping.warnings) or "- None"
    return f"""# Tiny Tapeout package — {info.project_name}

Generated from LibreLane Web run #{info.run_id} ({info.pdk}, LibreLane {info.librelane_version}).

This zip is a Tiny Tapeout project you can push to GitHub and submit at {info.pdk}:

1. Create a repo from the matching template (`ttsky` / `ttgf` / `ttihp`).
2. Copy `info.yaml`, `src/`, `test/`, and `docs/` from this zip into that repo.
3. Confirm `info.yaml` `top_module` is `{top}`.
4. Submit the GitHub URL at https://app.tinytapeout.com

After **Build shuttle GDS**, `gds/{top}.gds` is the layout hardened into the
Tiny Tapeout tile (official pins). Tiny Tapeout's GitHub flow may still
re-harden `src/` itself.

`hardened/` contains the GDS/LEF/netlist/timing views already produced by this run
(reference only).

## Warnings

{warns}

See PINOUT.md for the generated pad map.
"""


def _readme_caravel(info: DesignInfo, mapping: PortMap) -> str:
    warns = "\n".join(f"- {w}" for w in mapping.warnings) or "- None"
    return f"""# ChipFoundry (Caravel) package — {info.project_name}

Generated from LibreLane Web run #{info.run_id} on {info.pdk}.

ChipFoundry `cf push` requires `gds/user_project_wrapper.gds` plus `verilog/rtl/user_defines.v`.
LibreLane Web builds that wrapper GDS with **macro-first** hardening: your
hardened GDS/LEF is placed inside the 2920 x 3520 µm outline (a LEF is
generated automatically if the original run did not include Magic.WriteLEF).
The zip includes it at `gds/user_project_wrapper.gds` after **Build shuttle GDS** finishes.

1. Overlay these files onto [caravel_user_project](https://github.com/chipfoundry/caravel_user_project).
2. Run `cf precheck`, then `cf push` to https://platform.chipfoundry.io

GPIO power-on defaults are in `verilog/rtl/user_defines.v` (no `GPIO_MODE_INVALID`).

## Warnings

{warns}

See PINOUT.md for the generated pad map.
"""


def _readme_wafer(info: DesignInfo, mapping: PortMap) -> str:
    warns = "\n".join(f"- {w}" for w in mapping.warnings) or "- None"
    return f"""# wafer.chip package — {info.project_name}

Generated from LibreLane Web run #{info.run_id} on {info.pdk} (wafer.space GF180MCU MPW).

1. Overlay `src/chip_core.sv` and your RTL onto
   [gf180mcu-project-template](https://github.com/wafer-space/gf180mcu-project-template).
   Do not edit `chip_top.sv` (pad ring).
2. After **Build shuttle GDS**, `gds/chip_core.gds` is the hardened user core.
   wafer.space still wraps this in `chip_top` (pad ring) — this zip does not
   contain `chip_top.gds`.
3. Precheck: `python3 precheck.py --input chip_top.gds --slot 1x1`
4. Upload through https://platform.wafer.space

## Warnings

{warns}

See PINOUT.md for the generated pad map.
"""


def _readme_ihp(info: DesignInfo, gds_stem: str) -> str:
    return f"""# IHP Open MPW package — {info.project_name}

Generated from LibreLane Web run #{info.run_id} on {info.pdk}.

IHP Open MPW submissions are pull requests against the current `TO_<Month><Year>`
repository (see https://ihp-open-ip.readthedocs.io/en/latest/submission.html).

1. Fork the shuttle repo and copy the `{ident_slug(info.design_name)}/` directory
   from this zip next to `ExampleDesign`.
2. Confirm `metadata.json` and that the final GDS is
   `design_data/gds/{gds_stem}.gds` (FMD_QNC prefix is mandatory; **Build shuttle GDS**
   renames the GDS top cell to match).
3. Fill in any remaining author/contact fields, then open a signed-off pull request.

Sign-off reports are under `val/`. RTL sources are under `design_data/verilog/rtl/`.
"""


PACKERS = {
    "tinytapeout": pack_tinytapeout,
    "chipfoundry-caravel": pack_caravel,
    "wafer-space": pack_wafer,
    "ihp-openmpw": pack_ihp,
}


def build_package_files(
    run: object, target: FabTarget
) -> tuple[dict[str, bytes], list[str], DesignInfo]:
    from flow.services.fabpack.sources import file_source_for_run

    info = design_info_from_run(run)
    source = file_source_for_run(run)
    tree = WorkTree(source, info.design_name)
    views = resolve_views(tree)
    ports = parse_top_ports(tree, info, views)
    packer = PACKERS[target.id]
    files = packer(info, tree, views, ports)
    from flow.services.fabpack.harness import ihp_gds_stem, tt_top_name
    from flow.services.fabpack.integrate import load_foundry_gds

    foundry = load_foundry_gds(run, target.id)
    if foundry:
        if target.id == "tinytapeout":
            rel = f"gds/{tt_top_name(info.design_name)}.gds"
        elif target.id == "chipfoundry-caravel":
            rel = "gds/user_project_wrapper.gds"
        elif target.id == "wafer-space":
            rel = "gds/chip_core.gds"
        elif target.id == "ihp-openmpw":
            rel = f"{ident_slug(info.design_name)}/design_data/gds/{ihp_gds_stem(info.design_name)}.gds"
        else:
            rel = "gds/foundry.gds"
        files[rel] = foundry
        files["FOUNDRY_GDS.txt"] = (
            f"Shuttle top GDS (physical layout for this target) is {rel}\n".encode("utf-8")
        )
    warnings = list(views.missing_required())
    files["MANIFEST.json"] = dump_json(
        {
            "generator": info.tool_name,
            "librelane_version": info.librelane_version,
            "run_id": info.run_id,
            "design": info.design_name,
            "pdk": info.pdk,
            "pdk_family": info.pdk_family,
            "target": target.as_dict(),
            "files": sorted(files),
            "missing_required_views": warnings,
            "missing_views": [
                {"kind": k, "description": d, "required": req} for k, d, req in views.missing
            ],
            "metrics": summarize_metrics(merged_metrics(info)).as_dict(),
        }
    ).encode("utf-8")
    return files, warnings, info


def zip_package(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in sorted(files.items()):
            zf.writestr(name.replace("\\", "/"), payload)
    buffer.seek(0)
    return buffer.getvalue()
