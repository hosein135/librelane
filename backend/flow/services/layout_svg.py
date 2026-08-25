"""Generate vector SVG layout previews for the web UI."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "klayout_render_svg.py"


def get_layout_preview_source(
    instance, work_dir: Path | None
) -> dict[str, str] | None:
    """
    Path to the layout file used for KLayout preview (GDS preferred over DEF).
    """
    if instance.state_out is None:
        return None

    from librelane.state import DesignFormat

    state_out = instance.state_out
    input_view = state_out.get(DesignFormat.DEF)
    format_name = "DEF"
    if gds := state_out.get(DesignFormat.GDS):
        input_view = gds
        format_name = "GDSII"
    if input_view is None:
        return None

    path = Path(str(input_view)).expanduser()
    if not path.is_file():
        return None

    stored_path = str(path.resolve())
    if work_dir is not None:
        try:
            stored_path = path.resolve().relative_to(work_dir.resolve()).as_posix()
        except ValueError:
            pass

    return {
        "path": stored_path,
        "name": path.name,
        "format": format_name,
    }


def layout_view_changed(instance) -> bool:
    if instance.state_out is None:
        return False
    state_in = instance.state_in.result()
    from librelane.state import DesignFormat

    if instance.state_out.get(DesignFormat.DEF) != state_in.get(DesignFormat.DEF):
        return True
    if instance.state_out.get(DesignFormat.GDS) != state_in.get(DesignFormat.GDS):
        return True
    return False


def write_step_preview_svg(instance, work_dir: Path | None) -> str | None:
    """
    Write preview.svg into the step directory when the layout view changed.
    Returns a path relative to work_dir when possible.
    """
    if not layout_view_changed(instance):
        return None

    step_dir_raw = getattr(instance, "step_dir", None)
    if not step_dir_raw:
        return None

    step_dir = Path(step_dir_raw)
    if not step_dir.is_dir():
        return None

    output_path = step_dir / "preview.svg"
    if not _generate_svg_file(instance, output_path):
        return None

    if work_dir is not None:
        try:
            return output_path.relative_to(work_dir).as_posix()
        except ValueError:
            pass
    return str(output_path)


def _generate_svg_file(instance, output_path: Path) -> bool:
    from librelane.state import DesignFormat
    from librelane.steps import KLayout

    state_out = instance.state_out
    input_view = state_out.get(DesignFormat.DEF)
    if gds := state_out.get(DesignFormat.GDS):
        input_view = gds
    if input_view is None:
        return False

    try:
        render_step = KLayout.Render(instance.config, state_out, _config_quiet=True)
        render_step.step_dir = str(getattr(instance, "step_dir", output_path.parent))
        if getattr(instance, "toolbox", None) is not None:
            render_step.toolbox = instance.toolbox
        cmd = [
            sys.executable,
            str(_SCRIPT),
            os.path.abspath(str(input_view)),
            "--output",
            os.path.abspath(output_path),
            *render_step.get_cli_args(include_lefs=True),
        ]
        render_step.run_pya_script(cmd, silent=True)
    except Exception:
        return False

    return output_path.is_file() and output_path.stat().st_size > 0
