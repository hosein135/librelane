"""Implementation steps from notebook.ipynb, in execution order."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StepSpec:
    step_id: str
    title: str
    description: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    # First step uses empty State(); handled in runner.


NOTEBOOK_STEPS: list[StepSpec] = [
    StepSpec(
        "Yosys.Synthesis",
        "Synthesis",
        "Convert high-level Verilog to a gate-level netlist using Yosys.",
        {"VERILOG_FILES": None},  # filled per-run with absolute path to spm.v
    ),
    StepSpec(
        "OpenROAD.Floorplan",
        "Floorplan",
        "Determine chip dimensions and create the cell placement grid.",
    ),
    StepSpec(
        "OpenROAD.TapEndcapInsertion",
        "Tap / Endcap Insertion",
        "Place tap and endcap cells across the floorplan.",
    ),
    StepSpec(
        "OpenROAD.IOPlacement",
        "I/O Placement",
        "Place metal pins for top-level inputs and outputs.",
    ),
    StepSpec(
        "OpenROAD.GeneratePDN",
        "Generate PDN",
        "Create the power distribution network (smaller pitch for SPM).",
        {
            "FP_PDN_VWIDTH": 2,
            "FP_PDN_HWIDTH": 2,
            "FP_PDN_VPITCH": 30,
            "FP_PDN_HPITCH": 30,
        },
    ),
    StepSpec(
        "OpenROAD.GlobalPlacement",
        "Global Placement",
        "Assign fuzzy cell locations minimizing wirelength.",
    ),
    StepSpec(
        "OpenROAD.DetailedPlacement",
        "Detailed Placement",
        "Legalize placement to the site grid.",
    ),
    StepSpec(
        "OpenROAD.CTS",
        "Clock Tree Synthesis",
        "Build and place the clock tree buffers.",
    ),
    StepSpec(
        "OpenROAD.GlobalRouting",
        "Global Routing",
        "Plan routes between gates (no layout change).",
    ),
    StepSpec(
        "OpenROAD.DetailedRouting",
        "Detailed Routing",
        "Create physical metal wires (longest step).",
    ),
    StepSpec(
        "OpenROAD.FillInsertion",
        "Fill Insertion",
        "Insert decap and fill cells in empty sites.",
    ),
    StepSpec(
        "OpenROAD.RCX",
        "Parasitics Extraction (RCX)",
        "Extract parasitics into SPEF files.",
    ),
    StepSpec(
        "OpenROAD.STAPostPNR",
        "Static Timing Analysis (Post-PnR)",
        "Generate .lib and .sdf timing artifacts.",
    ),
    StepSpec(
        "KLayout.StreamOut",
        "Stream-out (GDSII)",
        "Export final GDSII for fabrication.",
    ),
    StepSpec(
        "Magic.DRC",
        "Design Rule Check (DRC)",
        "Verify manufacturability against foundry rules.",
    ),
    StepSpec(
        "Magic.SpiceExtraction",
        "SPICE Extraction",
        "Extract SPICE netlist from layout for LVS.",
    ),
    StepSpec(
        "Netgen.LVS",
        "Layout vs. Schematic (LVS)",
        "Compare physical implementation to logical netlist.",
    ),
]
