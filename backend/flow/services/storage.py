"""Storage quotas, free-space guards, and usage summaries."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from flow.models import FlowRun, FlowRunFile, User
from flow.services.workdir import (
    directory_size_bytes,
    remove_run_workdir,
    resolve_run_workdir,
    runs_root,
)


@dataclass(frozen=True)
class StorageSnapshot:
    user_disk_bytes: int
    user_db_bytes: int
    user_total_bytes: int
    user_quota_bytes: int
    run_budget_bytes: int
    min_free_bytes: int
    free_bytes: int
    retention_days: int
    runs_root: str

    def as_dict(self) -> dict:
        return {
            "user_disk_bytes": self.user_disk_bytes,
            "user_db_bytes": self.user_db_bytes,
            "user_total_bytes": self.user_total_bytes,
            "user_quota_bytes": self.user_quota_bytes,
            "run_budget_bytes": self.run_budget_bytes,
            "min_free_bytes": self.min_free_bytes,
            "free_bytes": self.free_bytes,
            "retention_days": self.retention_days,
            "runs_root": self.runs_root,
            "user_used_pct": (
                round(100.0 * self.user_total_bytes / self.user_quota_bytes, 1)
                if self.user_quota_bytes > 0
                else 0.0
            ),
        }


def _quota_bytes() -> int:
    return int(getattr(settings, "STORAGE_USER_QUOTA_BYTES", 20 * 1024**3))


def _run_budget_bytes() -> int:
    return int(getattr(settings, "STORAGE_RUN_BUDGET_BYTES", 10 * 1024**3))


def _min_free_bytes() -> int:
    return int(getattr(settings, "STORAGE_MIN_FREE_BYTES", 5 * 1024**3))


def _retention_days() -> int:
    return int(getattr(settings, "STORAGE_WORKDIR_RETENTION_DAYS", 0))


def free_disk_bytes(path: Path | None = None) -> int:
    target = path or runs_root()
    try:
        usage = shutil.disk_usage(target)
        return int(usage.free)
    except OSError:
        return 0


def user_disk_usage_bytes(user: User) -> int:
    total = 0
    for run in FlowRun.objects.filter(owner_user=user).only(
        "id", "owner_user_id", "work_dir", "temp_folder_name", "disk_bytes"
    ):
        if run.disk_bytes:
            total += int(run.disk_bytes)
            continue
        work = resolve_run_workdir(run)
        if work is not None:
            total += directory_size_bytes(work)
    return total


def user_db_usage_bytes(user: User) -> int:
    agg = FlowRunFile.objects.filter(run__owner_user=user).aggregate(total=Sum("size_bytes"))
    return int(agg["total"] or 0)


def storage_snapshot(user: User) -> StorageSnapshot:
    disk = user_disk_usage_bytes(user)
    db = user_db_usage_bytes(user)
    return StorageSnapshot(
        user_disk_bytes=disk,
        user_db_bytes=db,
        user_total_bytes=disk + db,
        user_quota_bytes=_quota_bytes(),
        run_budget_bytes=_run_budget_bytes(),
        min_free_bytes=_min_free_bytes(),
        free_bytes=free_disk_bytes(),
        retention_days=_retention_days(),
        runs_root=str(runs_root()),
    )


class StorageLimitError(RuntimeError):
    """Raised when a storage policy blocks creating or continuing a run."""


def assert_can_start_run(user: User) -> None:
    snap = storage_snapshot(user)
    if snap.free_bytes < snap.min_free_bytes:
        raise StorageLimitError(
            f"Not enough free disk under {snap.runs_root}: "
            f"{_fmt(snap.free_bytes)} free, need at least {_fmt(snap.min_free_bytes)}."
        )
    if snap.user_total_bytes >= snap.user_quota_bytes:
        raise StorageLimitError(
            f"Storage quota exceeded for your account: "
            f"using {_fmt(snap.user_total_bytes)} of {_fmt(snap.user_quota_bytes)} "
            f"(disk {_fmt(snap.user_disk_bytes)} + DB {_fmt(snap.user_db_bytes)}). "
            "Delete old runs to free space."
        )


def assert_can_continue_run(run: FlowRun) -> None:
    user = run.owner_user
    snap = storage_snapshot(user)
    if snap.free_bytes < snap.min_free_bytes:
        raise StorageLimitError(
            f"Not enough free disk to continue: "
            f"{_fmt(snap.free_bytes)} free, need at least {_fmt(snap.min_free_bytes)}."
        )

    work = resolve_run_workdir(run)
    run_disk = directory_size_bytes(work) if work is not None else int(run.disk_bytes or 0)
    if run_disk >= snap.run_budget_bytes:
        raise StorageLimitError(
            f"This run exceeds the per-run disk budget: "
            f"{_fmt(run_disk)} used, limit {_fmt(snap.run_budget_bytes)}."
        )

    # Remaining quota must leave room for growth; block if already over.
    other_disk = max(0, snap.user_disk_bytes - run_disk)
    projected = other_disk + run_disk + snap.user_db_bytes
    if projected >= snap.user_quota_bytes:
        raise StorageLimitError(
            f"Storage quota exceeded for your account: "
            f"using {_fmt(projected)} of {_fmt(snap.user_quota_bytes)}. "
            "Delete old runs to free space."
        )


def prune_expired_workdirs(*, dry_run: bool = False) -> list[int]:
    """
    Remove on-disk workdirs for finished runs older than retention days,
    keeping Postgres artifact rows. retention_days<=0 disables pruning.
    """
    days = _retention_days()
    if days <= 0:
        return []
    cutoff = timezone.now() - timedelta(days=days)
    pruned: list[int] = []
    qs = FlowRun.objects.filter(
        artifacts_stored=True,
        status__in=(FlowRun.Status.COMPLETED, FlowRun.Status.FAILED),
        updated_at__lt=cutoff,
    ).exclude(work_dir="")
    for run in qs:
        if dry_run:
            pruned.append(run.pk)
            continue
        remove_run_workdir(run)
        # remove_run_workdir clears work_dir; keep artifacts_stored True
        run.refresh_from_db()
        run.artifacts_stored = True
        run.save(update_fields=["artifacts_stored", "updated_at"])
        pruned.append(run.pk)
    return pruned


def _fmt(num_bytes: int) -> str:
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{num_bytes} B"
