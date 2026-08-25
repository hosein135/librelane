"""Temporary long-named work folders for LibreLane runs."""

from __future__ import annotations

import mimetypes
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.db import transaction

from flow.models import FlowRun, FlowRunFile

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def format_dir_timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def safe_fs_token(value: str, *, max_len: int = 80) -> str:
    cleaned = _SAFE_RE.sub("_", (value or "").strip().replace("/", "_").replace("\\", "_"))
    cleaned = cleaned.strip("._") or "run"
    return cleaned[:max_len]


def os_temp_root() -> Path:
    override = getattr(settings, "LIBRELANE_TEMP_ROOT", None)
    if override:
        root = Path(override).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        return root
    root = Path(tempfile.gettempdir()) / "librelane_runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def build_temp_folder_name(username: str, run_name: str) -> str:
    return (
        f"{safe_fs_token(username)}_"
        f"{safe_fs_token(run_name)}_"
        f"{format_dir_timestamp_utc()}"
    )


def create_run_temp_dir(username: str, run_name: str) -> tuple[Path, str]:
    folder_name = build_temp_folder_name(username, run_name)
    path = os_temp_root() / folder_name
    path.mkdir(parents=True, exist_ok=False)
    return path, folder_name


def resolve_temp_dir(folder_name: str) -> Path | None:
    if not folder_name:
        return None
    if ".." in folder_name or "/" in folder_name or "\\" in folder_name:
        return None
    path = (os_temp_root() / folder_name).resolve()
    try:
        path.relative_to(os_temp_root().resolve())
    except ValueError:
        return None
    return path


def remove_temp_dir(folder_name: str) -> None:
    path = resolve_temp_dir(folder_name)
    if path is None:
        return
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def remove_run_temp(run: FlowRun) -> None:
    if run.temp_folder_name:
        remove_temp_dir(run.temp_folder_name)
    if run.work_dir:
        work = Path(run.work_dir)
        root = os_temp_root().resolve()
        try:
            resolved = work.resolve()
            if resolved.exists() and str(resolved).startswith(str(root)):
                shutil.rmtree(resolved, ignore_errors=True)
        except OSError:
            pass
    run.work_dir = ""
    run.temp_folder_name = ""
    run.save(update_fields=["work_dir", "temp_folder_name", "updated_at"])


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
    if not run.work_dir:
        return 0
    work = Path(run.work_dir)
    if not work.is_dir():
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
    return stored


def finalize_run_workspace(run: FlowRun) -> None:
    """
    Persist generated files/folders into Postgres.

    The on-disk work folder is kept (no automatic deletion). Explicit run
    deletion still removes both DB rows and the work folder.
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
