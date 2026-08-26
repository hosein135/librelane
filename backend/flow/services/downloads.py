"""Build downloadable artifacts (ZIP) and inline SVG previews for flow step tabs."""

from __future__ import annotations

import base64
import io
import re
import zipfile
from pathlib import Path

from django.conf import settings
from django.http import Http404

from flow.models import FlowRun, FlowRunFile, FlowStepResult

_PREVIEW_IMG_RE = re.compile(
    r'<img\s+[^>]*src=["\']data:image/(?P<fmt>png|jpeg|jpg);base64,(?P<data>[^"\']+)["\']',
    re.IGNORECASE,
)


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _slugify_step_id(step_id: str) -> str:
    from librelane.common.misc import slugify

    return slugify(step_id)


def work_dir_for_run(run: FlowRun) -> Path | None:
    from flow.services.workdir import resolve_run_workdir

    resolved = resolve_run_workdir(run)
    if resolved is not None:
        return resolved
    if run.work_dir:
        candidate = Path(run.work_dir).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
    fallback = Path(settings.RUNS_DIR) / "runs" / f"user_{run.owner_user_id}" / f"run_{run.pk}"
    if fallback.is_dir():
        return fallback.resolve()
    return None


def discover_step_dir(run: FlowRun, step: FlowStepResult) -> Path | None:
    """Locate on-disk step output directory from work_dir or stored path."""
    output = step.output or {}
    slug = _slugify_step_id(step.step_id)
    order_prefix = f"{step.order + 1}-{slug}"

    raw = output.get("step_dir")
    if raw:
        candidate = Path(str(raw)).expanduser()
        if candidate.is_dir():
            return candidate.resolve()

    work = work_dir_for_run(run)
    if work is not None:
        direct = work / order_prefix
        if direct.is_dir():
            return direct.resolve()
        for child in work.iterdir():
            if child.is_dir() and slug in child.name:
                return child.resolve()

    return None


def resolve_file_path(path_value: str, work_dir: Path | None) -> Path | None:
    raw = path_value.replace("\\", "/").strip()
    candidate = Path(raw).expanduser()
    if candidate.is_file():
        return candidate.resolve()

    if work_dir is None:
        return None

    rel = raw.lstrip("/")
    if ".." in Path(rel).parts:
        return None
    joined = (work_dir / rel).resolve()
    if joined.is_file() and _path_within(joined, work_dir):
        return joined
    return None


def collect_step_files(step_dir: Path) -> list[Path]:
    """Every file under the step directory (all generated outputs)."""
    if not step_dir.is_dir():
        return []
    return sorted(
        (p.resolve() for p in step_dir.rglob("*") if p.is_file()),
        key=lambda p: str(p).lower(),
    )


def files_for_step_zip(run: FlowRun, step: FlowStepResult) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()

    step_dir = discover_step_dir(run, step)
    if step_dir is not None:
        for path in collect_step_files(step_dir):
            if path not in seen:
                seen.add(path)
                paths.append(path)

    work_dir = work_dir_for_run(run)
    output = step.output or {}
    for artifact in output.get("artifacts") or []:
        rel = artifact.get("path")
        if not rel:
            continue
        resolved = resolve_file_path(str(rel), work_dir)
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        paths.append(resolved)

    return sorted(paths, key=lambda p: str(p).lower())


def _step_dir_prefix(step: FlowStepResult) -> str:
    slug = _slugify_step_id(step.step_id)
    return f"{step.order + 1}-{slug}"


def db_files_for_step(run: FlowRun, step: FlowStepResult) -> list[FlowRunFile]:
    if not run.artifacts_stored:
        return []
    prefix = _step_dir_prefix(step)
    rows = list(FlowRunFile.objects.filter(run=run).order_by("relative_path"))
    matched = [r for r in rows if r.relative_path == prefix or r.relative_path.startswith(prefix + "/")]
    if matched:
        return matched
    # Fallback: any artifact path listed in step.output
    artifact_paths = {
        str(a.get("path", "")).replace("\\", "/").lstrip("/")
        for a in (step.output or {}).get("artifacts") or []
        if a.get("path")
    }
    return [r for r in rows if r.relative_path in artifact_paths]


def _is_directory_row(row: FlowRunFile) -> bool:
    return row.content_type == "inode/directory" or (
        row.size_bytes == 0 and bytes(row.content) == b"" and not Path(row.relative_path).suffix
    )


def build_step_zip(run: FlowRun, step: FlowStepResult) -> bytes:
    files = files_for_step_zip(run, step)
    db_files = [] if files else db_files_for_step(run, step)
    db_payload = [r for r in db_files if not _is_directory_row(r)]
    db_dirs = [r for r in db_files if _is_directory_row(r)]
    if not files and not db_payload and not db_dirs:
        raise Http404("No output files for this step.")

    work_dir = work_dir_for_run(run)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arcname = path.name
            if work_dir is not None:
                try:
                    arcname = path.relative_to(work_dir).as_posix()
                except ValueError:
                    try:
                        step_dir = discover_step_dir(run, step)
                        if step_dir is not None:
                            arcname = path.relative_to(step_dir).as_posix()
                            arcname = f"{step_dir.name}/{arcname}"
                    except ValueError:
                        pass
            zf.write(path, arcname=arcname)
        for row in db_dirs:
            zf.writestr(row.relative_path.rstrip("/") + "/", b"")
        for row in db_payload:
            zf.writestr(row.relative_path, bytes(row.content))
    buffer.seek(0)
    return buffer.getvalue()


def preview_svg_path(run: FlowRun, step: FlowStepResult) -> Path | None:
    step_dir = discover_step_dir(run, step)
    if step_dir is None:
        return None
    candidate = step_dir / "preview.svg"
    if candidate.is_file() and candidate.stat().st_size > 0:
        return candidate.resolve()

    output = step.output or {}
    rel = output.get("preview_svg")
    if rel:
        work = work_dir_for_run(run)
        resolved = resolve_file_path(str(rel), work)
        if resolved is not None:
            return resolved
    return None


def _png_path_in_step_dir(step_dir: Path) -> Path | None:
    for path in sorted(step_dir.rglob("*.png")):
        if path.is_file():
            return path
    return None


def _png_bytes_from_preview_html(preview_html: str) -> tuple[bytes, str] | None:
    match = _PREVIEW_IMG_RE.search(preview_html)
    if not match:
        return None
    fmt = match.group("fmt").lower()
    if fmt == "jpg":
        fmt = "jpeg"
    try:
        data = base64.b64decode(match.group("data"), validate=True)
    except Exception:
        return None
    if not data:
        return None
    return data, fmt


def _db_preview_svg_bytes(run: FlowRun, step: FlowStepResult) -> bytes | None:
    if not run.artifacts_stored:
        return None
    prefix = _step_dir_prefix(step)
    candidates = [
        f"{prefix}/preview.svg",
        "preview.svg",
    ]
    rel = (step.output or {}).get("preview_svg")
    if rel:
        candidates.insert(0, str(rel).replace("\\", "/").lstrip("/"))
    for path in candidates:
        try:
            row = FlowRunFile.objects.get(run=run, relative_path=path)
        except FlowRunFile.DoesNotExist:
            continue
        data = bytes(row.content)
        if data:
            return data
    return None


def build_preview_svg(run: FlowRun, step: FlowStepResult) -> bytes:
    on_disk = preview_svg_path(run, step)
    if on_disk is not None:
        return on_disk.read_bytes()

    from_db = _db_preview_svg_bytes(run, step)
    if from_db is not None:
        return from_db

    output = step.output or {}
    preview_html = output.get("preview_html") or ""
    image_bytes: bytes | None = None
    mime = "image/png"

    if preview_html:
        extracted = _png_bytes_from_preview_html(preview_html)
        if extracted:
            image_bytes, fmt = extracted
            mime = f"image/{fmt}"

    if image_bytes is None:
        step_dir = discover_step_dir(run, step)
        if step_dir is not None:
            png_path = _png_path_in_step_dir(step_dir)
            if png_path is not None:
                image_bytes = png_path.read_bytes()

    if not image_bytes:
        raise Http404("No layout preview for this step.")

    encoded = base64.b64encode(image_bytes).decode("ascii")
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1">\n'
        f'  <image xlink:href="data:{mime};base64,{encoded}" '
        'width="100%" height="100%" preserveAspectRatio="xMidYMid meet"/>\n'
        "</svg>\n"
    )
    return svg.encode("utf-8")


def _preview_source_from_state_json(step_dir: Path) -> dict[str, str] | None:
    state_path = step_dir / "state_out.json"
    if not state_path.is_file():
        return None
    try:
        import json

        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    for key, format_name in (("gds", "GDSII"), ("def", "DEF")):
        raw = data.get(key)
        if not raw:
            continue
        path = Path(str(raw)).expanduser()
        if path.is_file():
            return {"path": str(path.resolve()), "name": path.name, "format": format_name}
    return None


def preview_source_info(run: FlowRun, step: FlowStepResult) -> dict[str, str] | None:
    output = step.output or {}
    stored = output.get("preview_source")
    work_dir = work_dir_for_run(run)

    if isinstance(stored, dict) and stored.get("path"):
        resolved = resolve_file_path(str(stored["path"]), work_dir)
        if resolved is not None:
            return {
                "path": str(resolved),
                "name": stored.get("name") or resolved.name,
                "format": stored.get("format") or resolved.suffix.lstrip(".").upper(),
            }

    step_dir = discover_step_dir(run, step)
    if step_dir is not None:
        inferred = _preview_source_from_state_json(step_dir)
        if inferred is not None:
            return inferred
        for pattern in ("*.gds", "*.def"):
            matches = sorted(step_dir.glob(pattern))
            if matches:
                path = matches[-1].resolve()
                fmt = "GDSII" if path.suffix.lower() == ".gds" else "DEF"
                return {"path": str(path), "name": path.name, "format": fmt}

    return None


def preview_source_file(run: FlowRun, step: FlowStepResult) -> Path | None:
    info = preview_source_info(run, step)
    if info is None:
        return None
    path = Path(info["path"])
    return path if path.is_file() else None


def preview_source_db_file(run: FlowRun, step: FlowStepResult) -> FlowRunFile | None:
    if not run.artifacts_stored:
        return None
    info = (step.output or {}).get("preview_source")
    candidates: list[str] = []
    if isinstance(info, dict) and info.get("path"):
        candidates.append(str(info["path"]).replace("\\", "/").lstrip("/"))
        if info.get("name"):
            candidates.append(f"{_step_dir_prefix(step)}/{info['name']}")
    prefix = _step_dir_prefix(step)
    for pattern_suffix in (".gds", ".def"):
        candidates.append(f"{prefix}/*{pattern_suffix}")
    for row in FlowRunFile.objects.filter(run=run).order_by("relative_path"):
        if _is_directory_row(row):
            continue
        rel = row.relative_path
        if rel in candidates:
            return row
        if rel.startswith(prefix + "/") and rel.lower().endswith((".gds", ".def")):
            return row
    return None


def step_can_download_preview_source(run: FlowRun, step: FlowStepResult) -> bool:
    output = step.output or {}
    if not (
        output.get("preview_html")
        or output.get("preview_svg")
        or preview_svg_path(run, step) is not None
        or _db_preview_svg_bytes(run, step) is not None
    ):
        return False
    return preview_source_file(run, step) is not None or preview_source_db_file(run, step) is not None


def preview_source_download_filename(run: FlowRun, step: FlowStepResult) -> str:
    info = preview_source_info(run, step)
    if info and info.get("name"):
        return info["name"]
    return f"run-{run.pk}-step-{step.order}-layout.gds"


def step_can_download_zip(run: FlowRun, step: FlowStepResult) -> bool:
    if files_for_step_zip(run, step):
        return True
    return any(not _is_directory_row(r) for r in db_files_for_step(run, step))


def step_has_vector_preview(run: FlowRun, step: FlowStepResult) -> bool:
    path = preview_svg_path(run, step)
    if path is None:
        return False
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:4096]
    except OSError:
        return False
    return "<path " in head and "data:image" not in head


def zip_download_filename(run: FlowRun, step: FlowStepResult) -> str:
    slug = re.sub(r"[^\w.-]+", "_", step.step_id).strip("_") or f"step_{step.order}"
    return f"run-{run.pk}-step-{step.order}-{slug}.zip"


def svg_preview_filename(run: FlowRun, step: FlowStepResult) -> str:
    slug = re.sub(r"[^\w.-]+", "_", step.step_id).strip("_") or f"step_{step.order}"
    return f"run-{run.pk}-step-{step.order}-{slug}-preview.svg"


def collect_run_disk_files(run: FlowRun) -> list[Path]:
    work = work_dir_for_run(run)
    if work is None or not work.is_dir():
        return []
    return sorted(
        (p.resolve() for p in work.rglob("*") if p.is_file()),
        key=lambda p: str(p).lower(),
    )


def db_files_for_run(run: FlowRun) -> list[FlowRunFile]:
    if not run.artifacts_stored:
        return []
    return list(FlowRunFile.objects.filter(run=run).order_by("relative_path"))


def run_can_download_all_files(run: FlowRun) -> bool:
    """True only for completed runs that have at least one downloadable file."""
    if run.status != FlowRun.Status.COMPLETED:
        return False
    if run.artifacts_stored:
        return (
            FlowRunFile.objects.filter(run=run)
            .exclude(content_type="inode/directory")
            .exists()
        )
    work = work_dir_for_run(run)
    if work is None or not work.is_dir():
        return False
    # Cheap existence check — avoid walking the full tree on every /api/home poll.
    try:
        next(p for p in work.iterdir() if p.is_file() or p.is_dir())
        return True
    except StopIteration:
        return False


def run_zip_download_filename(run: FlowRun) -> str:
    design = re.sub(r"[^\w.-]+", "_", run.design_name or "design")
    design = design.strip("_") or "design"
    return f"run-{run.pk}-{design}-all-files.zip"


def build_run_zip(run: FlowRun) -> bytes:
    """Zip every file under the run workdir, or all stored FlowRunFile rows."""
    files = collect_run_disk_files(run)
    db_files = [] if files else db_files_for_run(run)
    db_payload = [r for r in db_files if not _is_directory_row(r)]
    db_dirs = [r for r in db_files if _is_directory_row(r)]
    if not files and not db_payload and not db_dirs:
        raise Http404("No output files for this run.")

    work_dir = work_dir_for_run(run)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arcname = path.name
            if work_dir is not None:
                try:
                    arcname = path.relative_to(work_dir).as_posix()
                except ValueError:
                    pass
            zf.write(path, arcname=arcname)
        for row in db_dirs:
            zf.writestr(row.relative_path.rstrip("/") + "/", b"")
        for row in db_payload:
            zf.writestr(row.relative_path, bytes(row.content))
    buffer.seek(0)
    return buffer.getvalue()
