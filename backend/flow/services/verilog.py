"""Validate Verilog sources for a run workdir (uploaded designs)."""

from __future__ import annotations

import re
from pathlib import Path

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_MODULE_RE = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
_ALLOWED_SUFFIXES = {".v", ".sv"}

MAX_VERILOG_FILES = 50
MAX_VERILOG_FILE_BYTES = 2 * 1024 * 1024
MAX_VERILOG_TOTAL_BYTES = 10 * 1024 * 1024

SOURCES_SUBDIR = "sources"


def strip_verilog_comments(source: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//.*?$", "", without_block, flags=re.MULTILINE)


def list_modules(source: str) -> list[str]:
    return _MODULE_RE.findall(strip_verilog_comments(source))


def is_valid_module_name(name: str) -> bool:
    return bool(name and _IDENT_RE.match(name))


def sanitize_verilog_filename(name: str) -> str:
    base = Path(str(name or "")).name.strip()
    if not base or base in {".", ".."} or ".." in base:
        raise ValueError(f"Invalid Verilog file name: {name!r}")
    suffix = Path(base).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise ValueError(f"{base}: expected a .v or .sv file.")
    if not _SAFE_NAME_RE.match(base):
        raise ValueError(
            f"{base}: use only letters, digits, '.', '_', '+', '-' in the file name."
        )
    return base


def sources_dir(work_dir: Path) -> Path:
    return Path(work_dir) / SOURCES_SUBDIR


def list_verilog_paths(work_dir: Path | None) -> list[Path]:
    if work_dir is None:
        return []
    root = Path(work_dir)
    src = sources_dir(root)
    search_roots = [src] if src.is_dir() else [root]
    paths: list[Path] = []
    for base in search_roots:
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if path.is_file() and path.suffix.lower() in _ALLOWED_SUFFIXES:
                paths.append(path)
    return paths


def read_sources_text(work_dir: Path | None) -> tuple[list[str], str]:
    """Return (file names, concatenated source for display)."""
    paths = list_verilog_paths(work_dir)
    names = [p.name for p in paths]
    chunks: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        header = f"// ===== {path.name} =====\n"
        chunks.append(header + text.rstrip() + "\n")
    return names, "\n".join(chunks)


def modules_in_workdir(work_dir: Path | None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for path in list_verilog_paths(work_dir):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for name in list_modules(source):
            if name not in seen:
                seen.add(name)
                found.append(name)
    return found


def require_top_module_in_workdir(top_module: str, work_dir: Path | None) -> list[Path]:
    """
    Ensure workdir has Verilog sources that declare ``module <top_module>``.

    Returns the list of Verilog paths. Raises ValueError with a user-facing message.
    """
    name = (top_module or "").strip()
    if not name:
        raise ValueError("Top module name is required.")
    if not is_valid_module_name(name):
        raise ValueError(
            f"Invalid top module name {name!r}. "
            "Use a Verilog identifier (letters, digits, underscore; not starting with a digit)."
        )

    paths = list_verilog_paths(work_dir)
    if not paths:
        raise ValueError(
            "No Verilog sources found for this run. "
            "Create a new run and upload .v / .sv files."
        )

    declared: list[str] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Could not read {path.name}: {exc}") from exc
        modules = list_modules(source)
        declared.extend(modules)
        if name in modules:
            return paths

    found = ", ".join(f"{m!r}" for m in sorted(set(declared))) if declared else "(none)"
    raise ValueError(
        f"Top module {name!r} was not found in uploaded Verilog. "
        f"Modules declared: {found}."
    )


def store_uploaded_verilog(
    work_dir: Path,
    uploads: list[tuple[str, bytes]],
) -> list[Path]:
    """
    Write uploaded Verilog bytes under ``work_dir/sources/``.

    ``uploads`` is a list of (original_filename, content_bytes).
    """
    if not uploads:
        raise ValueError("Upload at least one Verilog file (.v or .sv).")
    if len(uploads) > MAX_VERILOG_FILES:
        raise ValueError(f"Too many Verilog files (max {MAX_VERILOG_FILES}).")

    total = 0
    prepared: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for raw_name, data in uploads:
        name = sanitize_verilog_filename(raw_name)
        key = name.lower()
        if key in seen:
            raise ValueError(f"Duplicate file name: {name}")
        seen.add(key)
        if not data:
            raise ValueError(f"{name} is empty.")
        if len(data) > MAX_VERILOG_FILE_BYTES:
            raise ValueError(
                f"{name} exceeds {MAX_VERILOG_FILE_BYTES // (1024 * 1024)} MiB."
            )
        total += len(data)
        if b"\x00" in data:
            raise ValueError(f"{name} looks binary (contains null bytes).")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{name} is not valid UTF-8 text.") from exc
        if not list_modules(text):
            raise ValueError(f"{name}: no module declaration found.")
        prepared.append((name, data))

    if total > MAX_VERILOG_TOTAL_BYTES:
        raise ValueError(
            f"Total upload exceeds {MAX_VERILOG_TOTAL_BYTES // (1024 * 1024)} MiB."
        )

    dest = sources_dir(work_dir)
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, data in prepared:
        path = dest / name
        path.write_bytes(data)
        written.append(path)
    return written
