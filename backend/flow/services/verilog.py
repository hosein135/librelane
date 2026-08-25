"""Validate top-module names against Verilog sources under designs/."""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_MODULE_RE = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b")


def strip_verilog_comments(source: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//.*?$", "", without_block, flags=re.MULTILINE)


def list_modules(source: str) -> list[str]:
    return _MODULE_RE.findall(strip_verilog_comments(source))


def verilog_path_for(top_module: str, designs_dir: Path | None = None) -> Path:
    root = Path(designs_dir) if designs_dir is not None else Path(settings.DESIGNS_DIR)
    return root / f"{top_module}.v"


def require_top_module_verilog(
    top_module: str,
    *,
    designs_dir: Path | None = None,
) -> Path:
    """
    Ensure designs/<top_module>.v exists and declares ``module <top_module>``.

    Returns the Verilog path. Raises ValueError with a user-facing message otherwise.
    """
    name = (top_module or "").strip()
    if not name:
        raise ValueError("Top module name is required.")
    if not _IDENT_RE.match(name):
        raise ValueError(
            f"Invalid top module name {name!r}. "
            "Use a Verilog identifier (letters, digits, underscore; not starting with a digit)."
        )

    path = verilog_path_for(name, designs_dir)
    if not path.is_file():
        raise ValueError(
            f"Verilog file not found: {path.name} (expected under designs/). "
            f"Top module name must match the file basename."
        )

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Could not read {path.name}: {exc}") from exc

    modules = list_modules(source)
    if name not in modules:
        found = ", ".join(f"{m!r}" for m in modules) if modules else "(none)"
        raise ValueError(
            f"Top module {name!r} was not found in {path.name}. "
            f"Modules declared there: {found}."
        )
    return path
