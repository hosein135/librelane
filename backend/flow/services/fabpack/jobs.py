"""Background shuttle-top GDS jobs (LibreLane Classic).

Workers are started as *detached* subprocesses (new session) so they survive
Django runserver reloads, browser close, navigation, and logout. Progress is
tracked via ``job.json`` PID + DB state. Stale ``running`` markers are healed
automatically when the PID is no longer alive.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from django.conf import settings

from flow.models import FlowRun
from flow.services.fabpack.integrate import (
    cleanup_fab_workdir,
    extract_fatal_log_errors,
    extract_log_errors,
    fab_work_dir,
    load_foundry_gds,
    parse_fab_progress,
    read_job_log,
    read_job_state,
    save_job_log,
    save_job_state_db,
    write_job_state,
)
from flow.services.fabpack.targets import get_target

# In-memory handles for jobs started by *this* Django process (optional).
_fab_popen: dict[tuple[int, str], subprocess.Popen] = {}
_lock = threading.Lock()


def fab_job_key(run_id: int, target_id: str) -> tuple[int, str]:
    return (int(run_id), str(target_id))


def _manage_py() -> Path:
    return Path(settings.BASE_DIR) / "manage.py"


def _pid_alive(pid: int | None) -> bool:
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but not owned by us — treat as alive.
        return True
    except OSError:
        return False
    # On Windows, os.kill(pid, 0) may succeed for zombies differently; also
    # reject our own PID accidentally stored.
    if pid == os.getpid():
        return False
    return True


def _state_pid(state: dict | None) -> int | None:
    if not state:
        return None
    raw = state.get("pid")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _kill_pid_tree(pid: int) -> None:
    """Terminate a detached fab worker and its process group / children."""
    if not _pid_alive(pid):
        return
    if os.name != "nt":
        # Detached workers are session leaders (start_new_session=True).
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        deadline = time.time() + 8
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.2)
        if _pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + 8
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.2)
        if _pid_alive(pid):
            try:
                # SIGTERM may not exist meaningfully; use taskkill via shell.
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )
            except OSError:
                pass


def fab_job_is_running(run_id: int, target_id: str | None = None) -> bool:
    """True if a live fab worker exists (in-memory handle or persisted PID)."""
    with _lock:
        items = list(_fab_popen.items())
    for (rid, tid), proc in items:
        if rid != int(run_id):
            continue
        if target_id is not None and tid != str(target_id):
            continue
        if proc.poll() is None:
            return True
        with _lock:
            if _fab_popen.get((rid, tid)) is proc:
                _fab_popen.pop((rid, tid), None)

    if target_id is not None:
        try:
            run = FlowRun.objects.get(pk=run_id)
        except FlowRun.DoesNotExist:
            return False
        state = read_job_state(run, target_id)
        if state.get("status") == "running" and _pid_alive(_state_pid(state)):
            return True
        return False

    # Any target for this run.
    try:
        run = FlowRun.objects.get(pk=run_id)
    except FlowRun.DoesNotExist:
        return False
    # Scan workdirs + DB states for this run's fab_* folders is heavier;
    # check known targets via job state files under runs root.
    root = fab_work_dir(run, "_").parent
    prefix = f"run_{int(run_id)}_fab_"
    if root.is_dir():
        for child in root.iterdir():
            if not child.name.startswith(prefix):
                continue
            tid = child.name[len(prefix) :]
            state = read_job_state(run, tid)
            if state.get("status") == "running" and _pid_alive(_state_pid(state)):
                return True
    # Also DB-only running states (workdir already cleaned mid-flight is rare).
    from flow.models import FlowRunFile

    for row in FlowRunFile.objects.filter(
        run_id=run_id, relative_path__startswith="fabrication/", relative_path__endswith="/job.json"
    ):
        try:
            import json

            state = json.loads(bytes(row.content).decode("utf-8"))
        except Exception:
            continue
        if state.get("status") == "running" and _pid_alive(_state_pid(state)):
            return True
    return False


def user_has_fab_job(user_id: int) -> bool:
    with _lock:
        items = list(_fab_popen.items())
    for (rid, _tid), proc in items:
        if proc.poll() is not None:
            continue
        try:
            run = FlowRun.objects.get(pk=rid)
        except FlowRun.DoesNotExist:
            continue
        if run.owner_user_id == user_id:
            return True

    for run in FlowRun.objects.filter(owner_user_id=user_id).only("id"):
        if fab_job_is_running(run.pk):
            return True
    return False


def heal_stale_fab_job(run: FlowRun, target_id: str) -> dict | None:
    """
    If job.json/DB says running but the worker PID is dead (e.g. after an
    unexpected crash — not a normal Django reload, which detached workers
    survive), mark failed, persist log, and remove the worktree.
    """
    state = read_job_state(run, target_id)
    if state.get("status") != "running":
        return None
    if fab_job_is_running(run.pk, target_id):
        return None

    log_text = read_job_log(run, target_id)
    note = (
        "\n\n[Build worker is no longer running — the process exited unexpectedly "
        "or was killed. Generated files were removed. Click Rebuild to retry.]\n"
    )
    if log_text:
        log_text = log_text.rstrip() + note
    else:
        log_text = note.lstrip()

    failed = {
        "status": "failed",
        "target": target_id,
        "error": "Build worker stopped unexpectedly (process is no longer running).",
        "message": "Build worker stopped unexpectedly. Generated files were removed.",
        "pid": None,
    }
    try:
        from django.db import close_old_connections

        close_old_connections()
        save_job_log(run, target_id, log_text)
        save_job_state_db(run, target_id, failed)
    except Exception:
        # Still try to clear the on-disk stale marker.
        try:
            write_job_state(run, target_id, failed)
        except Exception:
            pass
    cleanup_fab_workdir(run, target_id)
    return failed


def start_fab_job(run: FlowRun, target_id: str, *, force: bool = False) -> dict:
    get_target(target_id, run.pdk)
    key = fab_job_key(run.pk, target_id)

    # Heal leftovers before starting / reporting already-running.
    heal_stale_fab_job(run, target_id)

    with _lock:
        existing = _fab_popen.get(key)
        if existing is not None and existing.poll() is None:
            return read_job_state(run, target_id)

        state = read_job_state(run, target_id)
        if state.get("status") == "running" and _pid_alive(_state_pid(state)):
            return state

        if not force and state.get("status") == "done":
            return state

        manage = _manage_py()
        if not manage.is_file():
            raise RuntimeError(f"manage.py not found at {manage}")

        # Detached session: survives Django runserver reloads and parent exit.
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0x00000008
            )

        popen_kwargs: dict[str, Any] = {
            "args": [
                sys.executable,
                str(manage),
                "run_fab_job",
                str(int(run.pk)),
                str(target_id),
            ],
            "cwd": str(manage.parent),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = creationflags
        else:
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(**popen_kwargs)
        running = {
            "status": "running",
            "target": target_id,
            "message": "Building shuttle top GDS (LibreLane chip-level flow)…",
            "pid": proc.pid,
            "started_at": time.time(),
        }
        write_job_state(run, target_id, running)
        try:
            save_job_state_db(run, target_id, running)
        except Exception:
            pass
        _fab_popen[key] = proc

        def _reap() -> None:
            try:
                proc.wait()
            finally:
                with _lock:
                    if _fab_popen.get(key) is proc:
                        _fab_popen.pop(key, None)

        threading.Thread(
            target=_reap, daemon=True, name=f"fab-reap-{run.pk}-{target_id}"
        ).start()

    return {
        "status": "running",
        "target": target_id,
        "message": "Building shuttle top GDS (LibreLane chip-level flow)…",
        "running": True,
        "pid": proc.pid,
    }


def stop_fab_job(run: FlowRun, target_id: str) -> dict:
    """Stop a running shuttle build and delete its generated worktree."""
    get_target(target_id, run.pdk)
    key = fab_job_key(run.pk, target_id)
    state = read_job_state(run, target_id)
    pid = _state_pid(state)

    with _lock:
        proc = _fab_popen.pop(key, None)

    log_text = read_job_log(run, target_id)

    if proc is not None and proc.poll() is None:
        try:
            _kill_pid_tree(proc.pid)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    elif pid:
        _kill_pid_tree(pid)

    if log_text and not log_text.rstrip().endswith("[Stopped by user]"):
        log_text = log_text.rstrip() + "\n\n[Stopped by user]\n"
    elif not log_text:
        log_text = "[Stopped by user]\n"

    stopped = {
        "status": "stopped",
        "target": target_id,
        "error": "Stopped by user.",
        "message": "Build stopped by user. Generated files were removed.",
        "pid": None,
    }
    try:
        from django.db import close_old_connections

        close_old_connections()
        save_job_log(run, target_id, log_text)
        save_job_state_db(run, target_id, stopped)
    except Exception:
        pass
    cleanup_fab_workdir(run, target_id)
    return fab_status(run, target_id)


def fab_status(run: FlowRun, target_id: str) -> dict:
    # Auto-heal orphaned "running" markers (dead PID / crashed worker).
    healed = heal_stale_fab_job(run, target_id)
    state = dict(healed) if healed else dict(read_job_state(run, target_id))

    running = fab_job_is_running(run.pk, target_id)
    if running:
        state["status"] = "running"
        state.setdefault("message", "Building shuttle top GDS…")
    state["running"] = running
    state["ready"] = (
        (not running)
        and state.get("status") == "done"
        and load_foundry_gds(run, target_id) is not None
    )
    log = read_job_log(run, target_id)
    state["log"] = log
    if running:
        state["errors"] = extract_fatal_log_errors(log)
    else:
        state["errors"] = extract_log_errors(log)
    if (
        state.get("status") in ("failed", "stopped")
        and not state.get("error")
        and state["errors"]
    ):
        state["error"] = state["errors"][-1]
    progress = parse_fab_progress(log, status=str(state.get("status") or ""))
    state.update(progress)
    if running and progress.get("progress_label"):
        state["message"] = progress["progress_label"]
    return state
