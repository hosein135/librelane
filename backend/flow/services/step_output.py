"""Format LibreLane step instances into structured web output."""

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path
from typing import Any

ARTIFACT_SUFFIXES = {
    ".rpt",
    ".json",
    ".v",
    ".def",
    ".gds",
    ".spef",
    ".sdf",
    ".log",
    ".csv",
    ".png",
    ".odb",
    ".mag",
}
ARTIFACT_NAMES = {"state_out.json", "config.json", "metrics.json", "or_metrics_out.json"}


def sanitize_for_json(value: Any) -> Any:
    """
    Recursively coerce values so they can be stored in Postgres jsonb.

    LibreLane metrics often include ±Infinity / NaN (e.g. unconstrained STA
    path groups). Python's json module accepts those; Postgres does not.
    """
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        try:
            as_float = float(value)
        except (OverflowError, ValueError):
            return None
        return as_float if math.isfinite(as_float) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_for_json(v) for v in value]
    # numpy scalars, etc.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return sanitize_for_json(item())
        except Exception:
            pass
    return str(value)


def _json_value(value: Any) -> Any:
    return sanitize_for_json(value)


def _views_updated(instance) -> list[str]:
    from librelane.state import DesignFormat

    if instance.state_out is None:
        return []

    state_in = instance.state_in.result()
    updated: list[str] = []
    for key, value in dict(instance.state_out).items():
        if value is None:
            continue
        if state_in.get(key) != value:
            df = DesignFormat.factory.get(key)
            updated.append(df.full_name if df is not None else str(key))
    return updated


def _metrics_updated(instance) -> dict[str, Any]:
    if instance.state_out is None:
        return {}

    state_in = instance.state_in.result()
    changes: dict[str, Any] = {}
    for key in instance.state_out.metrics:
        old = state_in.metrics.get(key)
        new = instance.state_out.metrics.get(key)
        if old != new:
            changes[str(key)] = _json_value(new)
    return changes


def _collect_artifacts(step_dir: Path | None, work_dir: Path | None) -> list[dict[str, Any]]:
    if step_dir is None or not step_dir.is_dir():
        return []

    artifacts: list[dict[str, Any]] = []
    for path in sorted(step_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in ARTIFACT_SUFFIXES and path.name not in ARTIFACT_NAMES:
            continue
        try:
            rel = str(path.relative_to(work_dir)) if work_dir else str(path)
        except ValueError:
            try:
                rel = str(path.relative_to(step_dir))
            except ValueError:
                rel = str(path)
        artifacts.append(
            {
                "name": path.name,
                "path": rel.replace("\\", "/"),
                "size": path.stat().st_size,
            }
        )
    return artifacts[:40]


def collect_step_file_paths(step_dir: Path | None, work_dir: Path | None) -> list[Path]:
    """All artifact files under a step directory (no display limit)."""
    if step_dir is None or not step_dir.is_dir():
        return []

    files: list[Path] = []
    for path in sorted(step_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in ARTIFACT_SUFFIXES and path.name not in ARTIFACT_NAMES:
            continue
        files.append(path)
    return files


def format_step_output(instance, work_dir: Path | None = None) -> dict[str, Any]:
    """Build structured output matching LibreLane notebook display()."""
    output: dict[str, Any] = {
        "step_id": getattr(instance, "id", ""),
        "step_name": getattr(instance, "name", ""),
        "elapsed_s": None,
        "views_updated": [],
        "metrics": {},
        "preview_html": None,
        "artifacts": [],
        "step_dir": None,
    }

    if instance.state_out is None:
        return output

    if instance.start_time is not None and instance.end_time is not None:
        output["elapsed_s"] = round(instance.end_time - instance.start_time, 2)

    output["views_updated"] = _views_updated(instance)
    output["metrics"] = _metrics_updated(instance)

    try:
        preview = instance.layout_preview()
        if preview:
            output["preview_html"] = preview
    except Exception:
        pass

    try:
        from flow.services.layout_svg import (
            get_layout_preview_source,
            write_step_preview_svg,
        )

        preview_svg = write_step_preview_svg(instance, work_dir)
        if preview_svg:
            output["preview_svg"] = preview_svg
        if output.get("preview_html") or output.get("preview_svg"):
            preview_source = get_layout_preview_source(instance, work_dir)
            if preview_source:
                output["preview_source"] = preview_source
    except Exception:
        pass

    step_dir = Path(instance.step_dir) if getattr(instance, "step_dir", None) else None
    if step_dir is not None:
        output["step_dir"] = str(step_dir)
        output["artifacts"] = _collect_artifacts(step_dir, work_dir)

    return sanitize_for_json(output)


def output_has_content(output: dict[str, Any] | None) -> bool:
    if not output:
        return False
    if output.get("error"):
        return True
    return any(
        [
            output.get("elapsed_s") is not None,
            bool(output.get("views_updated")),
            bool(output.get("metrics")),
            bool(output.get("preview_html")),
            bool(output.get("preview_svg")),
            bool(output.get("artifacts")),
        ]
    )


def format_step_summary_text(output: dict[str, Any]) -> str:
    """Short plain-text summary stored in FlowStepResult.summary."""
    parts: list[str] = []
    if output.get("elapsed_s") is not None:
        parts.append(f"Done in {output['elapsed_s']}s")
    if views := output.get("views_updated"):
        parts.append("updated " + ", ".join(views))
    if metrics := output.get("metrics"):
        parts.append(f"{len(metrics)} metric(s)")
    if output.get("preview_svg") or output.get("preview_html"):
        parts.append("layout preview")
    if artifacts := output.get("artifacts"):
        parts.append(f"{len(artifacts)} file(s)")
    return " · ".join(parts) if parts else "Step completed"
