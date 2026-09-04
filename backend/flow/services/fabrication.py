"""Public API for fabrication export (used by Django views)."""

from __future__ import annotations

import re

from django.http import Http404

from flow.models import FlowRun
from flow.services.fabpack.pack import build_package_files, zip_package
from flow.services.fabpack.targets import get_target, targets_for_pdk


def run_can_export_fabrication(run: FlowRun) -> bool:
    """Enabled once the flow has finished (zip build still validates files)."""
    return run.status == FlowRun.Status.COMPLETED


def fabrication_targets_for_run(run: FlowRun) -> list[dict]:
    return [t.as_dict() for t in targets_for_pdk(run.pdk)]


def fabrication_zip_filename(run: FlowRun, target_id: str) -> str:
    design = re.sub(r"[^\w.-]+", "_", run.design_name or "design").strip("_") or "design"
    target = re.sub(r"[^\w.-]+", "_", target_id or "target").strip("_") or "target"
    return f"run-{run.pk}-{design}-{target}-fabrication.zip"


def build_fabrication_zip(run: FlowRun, target_id: str) -> bytes:
    if run.status != FlowRun.Status.COMPLETED:
        raise Http404("Export to fabrication is only available for completed runs.")
    target = get_target(target_id, run.pdk)
    try:
        files, _missing, _info = build_package_files(run, target)
    except FileNotFoundError as exc:
        raise Http404(str(exc)) from exc
    if not files:
        raise Http404("No files were produced for this fabrication target.")
    return zip_package(files)
