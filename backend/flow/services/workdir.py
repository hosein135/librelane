"""Per-user / per-run workdirs under the project data directory."""

from __future__ import annotations

import mimetypes
import shutil
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Sum

from flow.models import FlowRun, FlowRunFile


def runs_root() -> Path:
    """Controlled root for all run workspaces (not /tmp)."""
    override = getattr(settings, "LIBRELANE_TEMP_ROOT", None) or ""
    if str(override).strip():
        root = Path(override).expanduser()
    else:
        root = Path(settings.RUNS_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def user_runs_dir(user_id: int) -> Path:
    path = runs_root() / f"user_{int(user_id)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_workdir_path(user_id: int, run_id: int) -> Path:
    return user_runs_dir(user_id) / f"run_{int(run_id)}"


def relative_workdir_key(user_id: int, run_id: int) -> str:
    return f"user_{int(user_id)}/run_{int(run_id)}"


def create_run_workdir(run: FlowRun) -> tuple[Path, str]:
    """
    Create ``$RUNS_ROOT/user_<uid>/run_<rid>/`` for this run.

    Returns (absolute path, relative key stored in temp_folder_name).
    """
    if not run.pk or not run.owner_user_id:
        raise ValueError("Run must be saved with an owner before creating a workdir.")
    path = run_workdir_path(run.owner_user_id, run.pk)
    path.mkdir(parents=True, exist_ok=True)
    key = relative_workdir_key(run.owner_user_id, run.pk)
    return path, key


def resolve_run_workdir(run: FlowRun) -> Path | None:
    """Resolve the on-disk workdir for a run if it exists and is under runs_root."""
    root = runs_root()
    candidates: list[Path] = []
    if run.work_dir:
        candidates.append(Path(run.work_dir).expanduser())
    if run.owner_user_id and run.pk:
        candidates.append(run_workdir_path(run.owner_user_id, run.pk))
    if run.temp_folder_name and "/" in run.temp_folder_name and ".." not in run.temp_folder_name:
        candidates.append(root / run.temp_folder_name)

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.is_dir():
            return resolved
    return None


def directory_size_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def measure_and_save_run_sizes(run: FlowRun) -> dict[str, int]:
    """Update disk_bytes / db_bytes on the run from disk + Postgres."""
    disk = 0
    work = resolve_run_workdir(run)
    if work is not None:
        disk = directory_size_bytes(work)
    db_agg = FlowRunFile.objects.filter(run=run).aggregate(total=Sum("size_bytes"))
    db = int(db_agg["total"] or 0)
    run.disk_bytes = disk
    run.db_bytes = db
    run.save(update_fields=["disk_bytes", "db_bytes", "updated_at"])
    return {"disk_bytes": disk, "db_bytes": db}


def remove_run_workdir(run: FlowRun) -> None:
    """Delete the on-disk worktree for this run (safe path check)."""
    root = runs_root()
    targets: list[Path] = []
    if run.owner_user_id and run.pk:
        targets.append(run_workdir_path(run.owner_user_id, run.pk))
    if run.work_dir:
        targets.append(Path(run.work_dir).expanduser())
    if run.temp_folder_name and "/" in run.temp_folder_name and ".." not in run.temp_folder_name:
        targets.append(root / run.temp_folder_name)

    seen: set[Path] = set()
    for target in targets:
        try:
            resolved = target.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            shutil.rmtree(resolved, ignore_errors=True)

    run.work_dir = ""
    run.temp_folder_name = ""
    run.disk_bytes = 0
    run.save(update_fields=["work_dir", "temp_folder_name", "disk_bytes", "updated_at"])


# Back-compat name used by views.delete_run
def remove_run_temp(run: FlowRun) -> None:
    remove_run_workdir(run)


def _guess_content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def store_run_files_in_db(run: FlowRun) -> int:
    """
    Persist the run work tree into Postgres (flow_run_files).

    Files are stored as BYTEA with relative paths. Directories are recorded as
    empty rows with content_type ``inode/directory`` so the folder layout is
    reconstructible even when a directory has no files.
    """
    work = resolve_run_workdir(run)
    if work is None:
        return 0

    stored = 0
    rows: list[FlowRunFile] = []
    seen: set[str] = set()

    for path in sorted(work.rglob("*"), key=lambda p: str(p).lower()):
        try:
            rel = path.relative_to(work).as_posix()
        except ValueError:
            continue
        if not rel or rel in seen or ".." in Path(rel).parts:
            continue

        if path.is_dir():
            seen.add(rel)
            rows.append(
                FlowRunFile(
                    run=run,
                    relative_path=rel,
                    content=b"",
                    size_bytes=0,
                    content_type="inode/directory",
                )
            )
            stored += 1
            continue

        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        seen.add(rel)
        rows.append(
            FlowRunFile(
                run=run,
                relative_path=rel,
                content=data,
                size_bytes=len(data),
                content_type=_guess_content_type(path),
            )
        )
        stored += 1

    with transaction.atomic():
        FlowRunFile.objects.filter(run=run).delete()
        if rows:
            FlowRunFile.objects.bulk_create(rows, batch_size=50)
        run.artifacts_stored = True
        run.save(update_fields=["artifacts_stored", "updated_at"])
    measure_and_save_run_sizes(run)
    return stored


def finalize_run_workspace(run: FlowRun) -> None:
    """
    Persist generated files/folders into Postgres and refresh size counters.

    The on-disk work folder is kept (no automatic deletion on finish).
    Retention pruning may remove old workdirs later if configured.
    """
    store_run_files_in_db(run)


def get_stored_file(run: FlowRun, relative_path: str) -> FlowRunFile | None:
    rel = relative_path.replace("\\", "/").lstrip("/")
    if ".." in rel:
        return None
    try:
        return FlowRunFile.objects.get(run=run, relative_path=rel)
    except FlowRunFile.DoesNotExist:
        return None
