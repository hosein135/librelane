"""FileSource implementations over a run workdir or archived FlowRunFile rows."""

from __future__ import annotations

from pathlib import Path

from flow.models import FlowRun, FlowRunFile
from flow.services.downloads import work_dir_for_run
from flow.services.fabpack.views import FileSource


class DiskFileSource:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def list(self) -> list[str]:
        files: list[str] = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            if rel.endswith(".pkl"):
                continue
            files.append(rel)
        return sorted(files)

    def read(self, rel_path: str) -> bytes:
        rel = rel_path.replace("\\", "/").lstrip("/")
        path = (self.root / rel).resolve()
        path.relative_to(self.root)
        return path.read_bytes()


class DbFileSource:
    def __init__(self, run: FlowRun) -> None:
        self._files: dict[str, bytes] = {}
        for row in FlowRunFile.objects.filter(run=run).order_by("relative_path"):
            rel = (row.relative_path or "").replace("\\", "/").lstrip("/")
            if not rel or row.content_type == "inode/directory":
                continue
            if rel.endswith(".pkl"):
                continue
            self._files[rel] = bytes(row.content)

    def list(self) -> list[str]:
        return sorted(self._files)

    def read(self, rel_path: str) -> bytes:
        return self._files[rel_path.replace("\\", "/").lstrip("/")]


def file_source_for_run(run: FlowRun) -> FileSource:
    work = work_dir_for_run(run)
    if work is not None and work.is_dir():
        return DiskFileSource(work)
    if run.artifacts_stored:
        return DbFileSource(run)
    raise FileNotFoundError(
        "This run has no work directory or stored artifacts to package."
    )
