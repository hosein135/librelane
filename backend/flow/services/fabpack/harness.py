"""Generate shuttle harness wrappers that instantiate the user's top module."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from flow.services.fabpack.verilog_ports import (
    ModulePorts,
    Port,
    looks_like_clock,
    reset_polarity,
)
from flow.services.fabpack.views import DesignInfo


def ident_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]", "_", value or "design")
    slug = re.sub(r"_+", "_", slug).strip("_") or "design"
    if slug[0].isdigit():
        slug = "u_" + slug
    return slug


def tt_top_name(design_name: str) -> str:
    slug = ident_slug(design_name)
    if slug.startswith("tt_um_"):
        return slug
    return f"tt_um_{slug}"


def ihp_gds_stem(design_name: str) -> str:
    slug = ident_slug(design_name).upper()
    if slug.startswith("FMD_QNC_"):
        return slug
    return f"FMD_QNC_{slug}"


@dataclass
class PinAssignment:
    harness: str
    design: str
    note: str = ""


@dataclass
class PortMap:
    clock: Port | None = None
    reset: Port | None = None
    reset_active_low: bool = True
    assignments: list[PinAssignment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    leftover_inputs: list[str] = field(default_factory=list)
    leftover_outputs: list[str] = field(default_factory=list)
    leftover_inouts: list[str] = field(default_factory=list)

    def pinout_rows(self) -> list[tuple[str, str]]:
        rows = [(a.harness, a.design) for a in self.assignments]
        if self.clock:
            rows.insert(0, ("clk", self.clock.name))
        if self.reset:
            polar = "active-low" if self.reset_active_low else "active-high (inverted)"
            rows.insert(1 if self.clock else 0, ("rst_n", f"{self.reset.name} ({polar})"))
        return rows


def classify_ports(
    ports: ModulePorts, preferred_clock: str | None = None
) -> tuple[Port | None, Port | None, list[Port], list[Port], list[Port]]:
    clock = None
    for port in ports.inputs():
        if looks_like_clock(port, preferred_clock):
            clock = port
            break
    reset = None
    for port in ports.inputs():
        if port is clock:
            continue
        if reset_polarity(port) is not None:
            reset = port
            break
    skip = {p.name for p in (clock, reset) if p is not None}
    power = {
        "vccd1",
        "vssd1",
        "vccd",
        "vssd",
        "vdd",
        "vss",
        "vpwr",
        "vgnd",
        "vpw",
        "vnw",
    }
    inputs = [p for p in ports.inputs() if p.name not in skip and p.name.lower() not in power]
    outputs = [p for p in ports.outputs() if p.name.lower() not in power]
    inouts = [p for p in ports.inouts() if p.name.lower() not in power]
    return clock, reset, inputs, outputs, inouts


def _bit_names(port: Port) -> list[str]:
    if not port.is_vector:
        return [port.name]
    assert port.msb is not None and port.lsb is not None
    step = 1 if port.msb >= port.lsb else -1
    return [f"{port.name}[{i}]" for i in range(port.lsb, port.msb + step, step)]


def _take(pool: list[str], count: int) -> list[str]:
    taken = pool[:count]
    del pool[:count]
    return taken


def _connect_bus(port: Port, wires: list[str], fill: str) -> str:
    bits = _bit_names(port)
    if len(wires) < len(bits):
        wires = wires + [fill] * (len(bits) - len(wires))
    if len(bits) == 1:
        return wires[0]
    if port.msb is not None and port.lsb is not None and port.msb >= port.lsb:
        ordered = list(reversed(wires[: len(bits)]))
    else:
        ordered = wires[: len(bits)]
    return "{" + ", ".join(ordered) + "}"


def _instance_ports(
    ports: ModulePorts,
    mapping: PortMap,
    *,
    clk_net: str,
    rst_net: str,
    input_pool: list[str],
    output_pool: list[str],
    inout_pool: list[str],
    unused_in: str = "1'b0",
) -> list[str]:
    lines: list[str] = []
    in_pool = list(input_pool)
    out_pool = list(output_pool)
    io_pool = list(inout_pool)

    for port in ports.ports:
        if mapping.clock is not None and port.name == mapping.clock.name:
            lines.append(f"    .{port.name}({clk_net})")
            continue
        if mapping.reset is not None and port.name == mapping.reset.name:
            lines.append(f"    .{port.name}({rst_net})")
            continue
        if port.name.lower() in {"vccd1", "vssd1", "vdd", "vss", "vpwr", "vgnd"}:
            continue
        width = port.width
        if port.direction == "input":
            taken = _take(in_pool, width)
            if len(taken) < width:
                extra = io_pool[: width - len(taken)]
                del io_pool[: len(extra)]
                taken.extend(extra)
            if len(taken) < width:
                mapping.leftover_inputs.append(port.name)
                mapping.warnings.append(
                    f"Input {port.name} ({width} bits) did not fit the harness; extra bits tied to 0."
                )
            for wire, design_bit in zip(taken, _bit_names(port)):
                mapping.assignments.append(PinAssignment(wire, design_bit))
            expr = _connect_bus(port, taken, unused_in)
            lines.append(f"    .{port.name}({expr})")
        elif port.direction == "output":
            taken = _take(out_pool, width)
            if len(taken) < width:
                extra = io_pool[: width - len(taken)]
                del io_pool[: len(extra)]
                taken.extend(extra)
            unused_names: list[str] = []
            if len(taken) < width:
                mapping.leftover_outputs.append(port.name)
                mapping.warnings.append(
                    f"Output {port.name} ({width} bits) did not fit the harness; extra bits left unconnected."
                )
                unused_names = [
                    f"unconnected_{ident_slug(port.name)}_{i}"
                    for i in range(width - len(taken))
                ]
                taken.extend(unused_names)
            for wire, design_bit in zip(taken, _bit_names(port)):
                if not wire.startswith("unconnected_"):
                    mapping.assignments.append(PinAssignment(wire, design_bit))
            expr = _connect_bus(port, taken, f"unconnected_{ident_slug(port.name)}")
            lines.append(f"    .{port.name}({expr})")
        else:
            taken = _take(io_pool, width)
            if len(taken) < width:
                mapping.leftover_inouts.append(port.name)
                mapping.warnings.append(
                    f"Inout {port.name} ({width} bits) did not fit the harness."
                )
                continue
            for wire, design_bit in zip(taken, _bit_names(port)):
                mapping.assignments.append(PinAssignment(wire, design_bit))
            expr = _connect_bus(port, taken, "/* unused */")
            lines.append(f"    .{port.name}({expr})")
    return lines


def looks_like_tt_top(ports: ModulePorts) -> bool:
    names = {p.name for p in ports.ports}
    required = {"ui_in", "uo_out", "uio_in", "uio_out", "uio_oe", "clk", "rst_n"}
    return required.issubset(names)


def looks_like_caravel_wrapper(ports: ModulePorts) -> bool:
    names = {p.name for p in ports.ports}
    return {"wb_clk_i", "io_in", "io_out", "io_oeb"}.issubset(names)


def generate_tt_wrapper(
    info: DesignInfo, ports: ModulePorts | None
) -> tuple[str, str, PortMap]:
    top = tt_top_name(info.design_name)
    mapping = PortMap()
    if ports is None:
        mapping.warnings.append(
            "Could not parse top-module ports; wrapper is a passthrough stub."
        )
        return top, _tt_stub(top), mapping

    if looks_like_tt_top(ports) and ports.module == top:
        mapping.warnings.append(
            "Top module already matches the Tiny Tapeout pinout; no extra wrapper generated."
        )
        return top, "", mapping

    clock, reset, inputs, outputs, inouts = classify_ports(ports, info.clock_port)
    mapping.clock = clock
    mapping.reset = reset
    mapping.reset_active_low = True if reset is None else reset_polarity(reset) != "high"
    if clock is None:
        mapping.warnings.append(
            "No clock port detected; Tiny Tapeout clk is unused by the design."
        )
    if reset is None:
        mapping.warnings.append(
            "No reset port detected; Tiny Tapeout rst_n is unused by the design."
        )

    ui = [f"ui_in[{i}]" for i in range(8)]
    uo = [f"uo_out[{i}]" for i in range(8)]
    uio = [f"uio_in[{i}]" for i in range(8)]
    rst_net = "rst_n" if mapping.reset_active_low else "~rst_n"
    inst_ports = _instance_ports(
        ports,
        mapping,
        clk_net="clk",
        rst_net=rst_net,
        input_pool=ui,
        output_pool=uo,
        inout_pool=uio,
    )
    unused_decls = _unconnected_decls(inst_ports)
    used_uo = {a.harness for a in mapping.assignments if a.harness.startswith("uo_out[")}
    uo_tie = "\n".join(
        f"    assign uo_out[{i}] = 1'b0;" for i in range(8) if f"uo_out[{i}]" not in used_uo
    )
    body = f"""`default_nettype none
/*
 * Tiny Tapeout wrapper generated by LibreLane Web for {info.design_name}.
 * SPDX-License-Identifier: Apache-2.0
 */
module {top} (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
    assign uio_out = 8'h00;
    assign uio_oe  = 8'h00;
{uo_tie}
{unused_decls}
    {ports.module} user (
{_join_ports(inst_ports)}
    );

    wire _unused = &{{ena, 1'b0}};

endmodule
"""
    return top, body, mapping


def _join_ports(inst_ports: list[str]) -> str:
    return ",\n".join(inst_ports)


def _unconnected_decls(inst_ports: list[str]) -> str:
    names = sorted(set(re.findall(r"unconnected_[A-Za-z0-9_]+", "\n".join(inst_ports))))
    if not names:
        return ""
    return "\n".join(f"    wire {n};" for n in names) + "\n"


def _tt_stub(top: str) -> str:
    return f"""`default_nettype none
module {top} (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
    assign uo_out = ui_in;
    assign uio_out = 8'h00;
    assign uio_oe = 8'h00;
    wire _unused = &{{ena, clk, rst_n, uio_in, 1'b0}};
endmodule
"""


def generate_caravel_wrapper(
    info: DesignInfo, ports: ModulePorts | None
) -> tuple[str, PortMap]:
    mapping = PortMap()
    if ports is None:
        mapping.warnings.append("Could not parse top-module ports; wrapper instantiates nothing.")
        return _caravel_empty(), mapping
    if looks_like_caravel_wrapper(ports):
        mapping.warnings.append("Top module already looks like user_project_wrapper.")
        return "", mapping

    clock, reset, inputs, outputs, inouts = classify_ports(ports, info.clock_port)
    mapping.clock = clock
    mapping.reset = reset
    mapping.reset_active_low = True if reset is None else reset_polarity(reset) != "high"

    in_bits = sum(p.width for p in inputs)
    out_bits = sum(p.width for p in outputs)
    inout_bits = sum(p.width for p in inouts)
    pad = 5  # GPIO 0–4 are reserved for Caravel management.
    in_pool = [f"io_in[{i}]" for i in range(pad, min(38, pad + in_bits))]
    pad += len(in_pool)
    out_pool = [f"user_io_out[{i}]" for i in range(pad, min(38, pad + out_bits))]
    out_pads = list(range(pad, min(38, pad + out_bits)))
    pad += len(out_pool)
    io_pool = [f"io_in[{i}]" for i in range(pad, min(38, pad + inout_bits))]
    rst_net = "~wb_rst_i" if mapping.reset_active_low else "wb_rst_i"
    inst_ports = _instance_ports(
        ports,
        mapping,
        clk_net="wb_clk_i",
        rst_net=rst_net,
        input_pool=in_pool,
        output_pool=out_pool,
        inout_pool=io_pool,
    )
    oeb = ["1'b1"] * 38
    for i in out_pads:
        oeb[i] = "1'b0"
    unused_out = "\n".join(
        f"    assign user_io_out[{i}] = 1'b0;"
        for i in range(38)
        if i not in set(out_pads)
    )
    needed = in_bits + out_bits + inout_bits
    if needed > 33:
        mapping.warnings.append(
            f"Design has {needed} signal bits; Caravel user GPIOs 5–37 provide 33 pads. Extra bits were tied off."
        )
    unused_decls = _unconnected_decls(inst_ports)
    verilog = f"""`default_nettype none
/*
 * Caravel user_project_wrapper generated by LibreLane Web for {info.design_name}.
 * SPDX-License-Identifier: Apache-2.0
 */
module user_project_wrapper #(
    parameter BITS = 32
) (
`ifdef USE_POWER_PINS
    inout vdda1,
    inout vdda2,
    inout vssa1,
    inout vssa2,
    inout vccd1,
    inout vccd2,
    inout vssd1,
    inout vssd2,
`endif
    input wb_clk_i,
    input wb_rst_i,
    input wbs_stb_i,
    input wbs_cyc_i,
    input wbs_we_i,
    input [3:0] wbs_sel_i,
    input [31:0] wbs_dat_i,
    input [31:0] wbs_adr_i,
    output wbs_ack_o,
    output [31:0] wbs_dat_o,
    input  [127:0] la_data_in,
    output [127:0] la_data_out,
    input  [127:0] la_oenb,
    input  [37:0] io_in,
    output [37:0] io_out,
    output [37:0] io_oeb,
    inout  [28:0] analog_io,
    input user_clock2,
    output [2:0] user_irq
);
    wire [37:0] user_io_out;
    assign io_out = user_io_out;
{unused_out}
    assign wbs_ack_o = 1'b0;
    assign wbs_dat_o = 32'h0000_0000;
    assign la_data_out = 128'h0;
    assign user_irq = 3'b000;
    assign analog_io = 29'bz;
    assign io_oeb = {{{", ".join(reversed(oeb))}}};
{unused_decls}
    {ports.module} user (
`ifdef USE_POWER_PINS
        .VPWR(vccd1),
        .VGND(vssd1),
`endif
{_join_ports(inst_ports)}
    );

    wire _unused = &{{user_clock2, wbs_stb_i, wbs_cyc_i, wbs_we_i, wbs_sel_i,
                      wbs_dat_i, wbs_adr_i, la_data_in, la_oenb, 1'b0}};

endmodule
"""
    return verilog, mapping


def _caravel_empty() -> str:
    return """`default_nettype none
module user_project_wrapper (
`ifdef USE_POWER_PINS
    inout vccd1, inout vssd1,
`endif
    input wb_clk_i,
    input wb_rst_i,
    input wbs_stb_i,
    input wbs_cyc_i,
    input wbs_we_i,
    input [3:0] wbs_sel_i,
    input [31:0] wbs_dat_i,
    input [31:0] wbs_adr_i,
    output wbs_ack_o,
    output [31:0] wbs_dat_o,
    input  [127:0] la_data_in,
    output [127:0] la_data_out,
    input  [127:0] la_oenb,
    input  [37:0] io_in,
    output [37:0] io_out,
    output [37:0] io_oeb,
    inout  [28:0] analog_io,
    input user_clock2,
    output [2:0] user_irq
);
    assign wbs_ack_o = 1'b0;
    assign wbs_dat_o = 32'h0;
    assign la_data_out = 128'h0;
    assign io_out = 38'h0;
    assign io_oeb = {38{1'b1}};
    assign analog_io = 29'bz;
    assign user_irq = 3'b0;
    wire _unused = &{wb_clk_i, wb_rst_i, wbs_stb_i, wbs_cyc_i, wbs_we_i,
                     wbs_sel_i, wbs_dat_i, wbs_adr_i, la_data_in, la_oenb,
                     io_in, user_clock2, 1'b0};
endmodule
"""


def generate_chip_core(
    info: DesignInfo, ports: ModulePorts | None
) -> tuple[str, PortMap]:
    """wafer.chip (wafer.space) ``chip_core`` with the user design inside."""
    mapping = PortMap()
    num_in, num_bidir, num_analog = 12, 40, 2
    if ports is None:
        mapping.warnings.append("Could not parse top-module ports; chip_core is a stub.")
        user_inst = "    assign bidir_out = '0;\n"
    else:
        clock, reset, inputs, outputs, inouts = classify_ports(ports, info.clock_port)
        mapping.clock = clock
        mapping.reset = reset
        mapping.reset_active_low = True if reset is None else reset_polarity(reset) != "high"
        in_pool = [f"input_in[{i}]" for i in range(num_in)]
        out_pool = [f"bidir_out[{i}]" for i in range(num_bidir)]
        io_pool = [f"analog[{i}]" for i in range(num_analog)]
        rst_net = "rst_n" if mapping.reset_active_low else "~rst_n"
        inst_ports = _instance_ports(
            ports,
            mapping,
            clk_net="clk",
            rst_net=rst_net,
            input_pool=in_pool,
            output_pool=out_pool,
            inout_pool=io_pool,
        )
        unused_decls = _unconnected_decls(inst_ports)
        used_out = {
            a.harness for a in mapping.assignments if a.harness.startswith("bidir_out[")
        }
        tie = "\n".join(
            f"    assign bidir_out[{i}] = 1'b0;"
            for i in range(num_bidir)
            if f"bidir_out[{i}]" not in used_out
        )
        user_inst = f"""{unused_decls}{tie}
    {ports.module} user (
{_join_ports(inst_ports)}
    );
"""
    body = f"""`default_nettype none
// wafer.chip (wafer.space) chip_core generated by LibreLane Web for {info.design_name}.
// SPDX-License-Identifier: Apache-2.0
module chip_core #(
    parameter NUM_INPUT_PADS = {num_in},
    parameter NUM_BIDIR_PADS = {num_bidir},
    parameter NUM_ANALOG_PADS = {num_analog}
) (
`ifdef USE_POWER_PINS
    inout wire VDD,
    inout wire VSS,
`endif
    input  wire clk,
    input  wire rst_n,
    input  wire [NUM_INPUT_PADS-1:0] input_in,
    output wire [NUM_INPUT_PADS-1:0] input_pu,
    output wire [NUM_INPUT_PADS-1:0] input_pd,
    input  wire [NUM_BIDIR_PADS-1:0] bidir_in,
    output wire [NUM_BIDIR_PADS-1:0] bidir_out,
    output wire [NUM_BIDIR_PADS-1:0] bidir_oe,
    output wire [NUM_BIDIR_PADS-1:0] bidir_cs,
    output wire [NUM_BIDIR_PADS-1:0] bidir_sl,
    output wire [NUM_BIDIR_PADS-1:0] bidir_ie,
    output wire [NUM_BIDIR_PADS-1:0] bidir_pu,
    output wire [NUM_BIDIR_PADS-1:0] bidir_pd,
    inout  wire [NUM_ANALOG_PADS-1:0] analog
);
    assign input_pu = '0;
    assign input_pd = '0;
    assign bidir_oe = '1;
    assign bidir_cs = '0;
    assign bidir_sl = '0;
    assign bidir_ie = ~bidir_oe;
    assign bidir_pu = '0;
    assign bidir_pd = '0;
{user_inst}
    wire _unused = &{{bidir_in, analog, 1'b0}};
endmodule
"""
    return body, mapping


def generate_user_defines(mapping: PortMap) -> str:
    """Caravel GPIO power-on defaults for pads 5–37 (0–4 are fixed)."""
    used_out: set[int] = set()
    used_in: set[int] = set()
    for assign in mapping.assignments:
        m = re.match(r"(?:user_)?io_(in|out)\[(\d+)\]", assign.harness)
        if not m:
            continue
        idx = int(m.group(2))
        if m.group(1) == "out":
            used_out.add(idx)
        else:
            used_in.add(idx)
    lines = [
        "// SPDX-License-Identifier: Apache-2.0",
        "`ifndef __USER_DEFINES_H",
        "`define __USER_DEFINES_H",
        "`define GPIO_MODE_USER_STD_INPUT_NOPULL 13'h0402",
        "`define GPIO_MODE_USER_STD_OUTPUT 13'h1808",
        "`define GPIO_MODE_USER_STD_BIDIRECTIONAL 13'h1800",
        "`define GPIO_MODE_MGMT_STD_INPUT_NOPULL 13'h0403",
        "",
    ]
    for i in range(5, 38):
        if i in used_out:
            mode = "`GPIO_MODE_USER_STD_OUTPUT"
        elif i in used_in:
            mode = "`GPIO_MODE_USER_STD_INPUT_NOPULL"
        else:
            mode = "`GPIO_MODE_MGMT_STD_INPUT_NOPULL"
        lines.append(f"`define USER_CONFIG_GPIO_{i}_INIT {mode}")
    lines.append("`endif")
    lines.append("")
    return "\n".join(lines)
