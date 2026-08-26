"""Run LibreLane steps sequentially, matching notebook.ipynb."""

from __future__ import annotations

import io
import traceback
from contextlib import redirect_stderr, redirect_stdout
import pickle
from pathlib import Path

from django.utils import timezone as dj_timezone

from flow.models import FlowRun, FlowStepResult, User
from flow.services.setup import configure_interactive, pdk_is_ready
from flow.services.step_output import format_step_output, format_step_summary_text
from flow.services.storage import StorageLimitError, assert_can_continue_run
from flow.services.verilog import list_verilog_paths, require_top_module_in_workdir
from flow.services.workdir import create_run_workdir, finalize_run_workspace, measure_and_save_run_sizes
from flow.steps import NOTEBOOK_STEPS, StepSpec


class FlowRunner:
    def __init__(self, run: FlowRun) -> None:
        self.run = run
        self.work_dir: Path | None = Path(run.work_dir) if run.work_dir else None
        self._state_file: Path | None = (
            self.work_dir / "librelane_state.pkl" if self.work_dir else None
        )
        self._state = self._load_state()

    def _owner_username(self) -> str:
        try:
            return self.run.owner_user.username
        except User.DoesNotExist:
            return "user"

    def _ensure_work_dir(self) -> Path:
        if self.work_dir is not None and self.work_dir.is_dir():
            return self.work_dir
        return self._prepare_work_dir()

    def _prepare_work_dir(self) -> Path:
        if self.run.work_dir:
            existing = Path(self.run.work_dir)
            if existing.is_dir():
                require_top_module_in_workdir(self.run.design_name, existing)
                self.work_dir = existing
                self._state_file = existing / "librelane_state.pkl"
                return existing

        assert_can_continue_run(self.run)
        base, folder_key = create_run_workdir(self.run)
        require_top_module_in_workdir(self.run.design_name, base)

        self.run.work_dir = str(base)
        self.run.temp_folder_name = folder_key
        self.run.artifacts_stored = False
        self.run.save(
            update_fields=["work_dir", "temp_folder_name", "artifacts_stored", "updated_at"]
        )
        self.work_dir = base
        self._state_file = base / "librelane_state.pkl"
        return base

    def _save_setup_progress(self, log: io.StringIO, message: str = "") -> None:
        if message:
            log.write(message)
            if not message.endswith("\n"):
                log.write("\n")
        self.run.setup_log = log.getvalue()
        self.run.save(update_fields=["setup_log", "updated_at"])

    def setup(self) -> None:
        log = io.StringIO()
        self.run.status = FlowRun.Status.SETTING_UP
        self.run.error_message = ""
        self.run.save(update_fields=["status", "error_message", "updated_at"])

        try:
            from django.conf import settings
            from flow.services.setup import check_tkinter

            self._save_setup_progress(log, f"LibreLane {self._version()}")
            self._save_setup_progress(log, "Checking storage limits…")
            assert_can_continue_run(self.run)
            self._save_setup_progress(log, "Checking top module in uploaded Verilog…")
            work = self._ensure_work_dir()
            require_top_module_in_workdir(self.run.design_name, work)
            self._save_setup_progress(log, "Checking environment…")
            check_tkinter()
            pdk_root = self.run.pdk_root or settings.PDK_ROOT
            if pdk_is_ready(pdk_root, self.run.pdk_family):
                self._save_setup_progress(
                    log,
                    f"PDK «{self.run.pdk_family}» already enabled under {pdk_root}.",
                )
            else:
                self._save_setup_progress(
                    log,
                    "PDKs are not ready yet. Run ./run.sh (or ./dev_run.sh) once and wait for "
                    "“PDKs ready” in the terminal before using Setup PDK here.",
                )
                raise RuntimeError(
                    "PDKs not downloaded. Run ./run.sh and wait for the first-startup PDK "
                    "download to finish in the terminal."
                )
            self._save_setup_progress(log, "Configuring flow…")
            configure_interactive(
                self.run.design_name,
                pdk=self.run.pdk,
                clock_period=self.run.clock_period,
            )
            self._seed_step_rows()
            self.run.setup_log = log.getvalue()
            self.run.status = FlowRun.Status.READY
            self.run.error_message = ""
        except Exception as exc:
            self.run.setup_log = log.getvalue() + "\n" + traceback.format_exc()
            self.run.status = FlowRun.Status.FAILED
            self.run.error_message = str(exc)
            raise
        finally:
            self.run.save()

    def _ensure_interactive_config(self) -> None:
        """Re-apply LibreLane Config from DB fields (needed after process restart)."""
        configure_interactive(
            self.run.design_name,
            pdk=self.run.pdk,
            clock_period=self.run.clock_period,
        )

    def run_all(self) -> None:
        try:
            # Resume incomplete flows without re-setup when the workdir already exists.
            needs_setup = self.run.status == FlowRun.Status.PENDING or not self._workdir_ready()
            if needs_setup:
                self.setup()
            else:
                # Setup already ran earlier; Config.current_interactive is process-local
                # and is lost after a server restart — restore from FlowRun scalars.
                self._ensure_interactive_config()
                self._seed_step_rows()

            self.run.status = FlowRun.Status.RUNNING
            self.run.error_message = ""
            self.run.save(update_fields=["status", "error_message", "updated_at"])

            # Pick up LibreLane state from the last successful step.
            self._state = self._load_state()

            for index, spec in enumerate(NOTEBOOK_STEPS):
                step_row = self.run.steps.get(order=index)
                if step_row.status in (
                    FlowStepResult.Status.DONE,
                    FlowStepResult.Status.SKIPPED,
                ):
                    continue
                self.run.current_step_index = index
                self.run.save(update_fields=["current_step_index", "updated_at"])
                self._run_single(step_row, spec)

            self.run.refresh_from_db()
            if all(
                s.status in (FlowStepResult.Status.DONE, FlowStepResult.Status.SKIPPED)
                for s in self.run.steps.all()
            ):
                self.run.status = FlowRun.Status.COMPLETED
            else:
                # Should only happen if a step failed and raised into finally.
                if self.run.status != FlowRun.Status.FAILED:
                    self.run.status = FlowRun.Status.READY
            self.run.save(update_fields=["status", "updated_at"])
        finally:
            self._finalize_if_terminal()

    def run_step(self, order: int) -> None:
        try:
            if self.run.status == FlowRun.Status.PENDING or not self._workdir_ready():
                self.setup()
            else:
                self._ensure_interactive_config()

            spec = NOTEBOOK_STEPS[order]
            step_row = self.run.steps.get(order=order)
            self.run.status = FlowRun.Status.RUNNING
            self.run.current_step_index = order
            self.run.error_message = ""
            self.run.save(
                update_fields=["status", "current_step_index", "error_message", "updated_at"]
            )
            self._run_single(step_row, spec)

            if all(
                s.status in (FlowStepResult.Status.DONE, FlowStepResult.Status.SKIPPED)
                for s in self.run.steps.all()
            ):
                self.run.status = FlowRun.Status.COMPLETED
            else:
                self.run.status = FlowRun.Status.READY
            self.run.save(update_fields=["status", "updated_at"])
        finally:
            self._finalize_if_terminal()

    def _workdir_ready(self) -> bool:
        if not self.run.work_dir:
            return False
        try:
            return Path(self.run.work_dir).expanduser().is_dir()
        except OSError:
            return False

    def _finalize_if_terminal(self) -> None:
        self.run.refresh_from_db()
        # Only archive + delete disk after a fully successful run so failed
        # flows can still resume from on-disk LibreLane state.
        if self.run.status == FlowRun.Status.COMPLETED:
            finalize_run_workspace(self.run)

    def _run_single(self, step_row: FlowStepResult, spec: StepSpec) -> None:
        from librelane.state import State
        from librelane.steps import Step

        assert_can_continue_run(self.run)
        work_dir = self._ensure_work_dir()

        step_row.status = FlowStepResult.Status.RUNNING
        step_row.started_at = dj_timezone.now()
        step_row.log = ""
        step_row.save()

        log_capture = io.StringIO()
        try:
            step_cls = Step.factory.get(spec.step_id)
            kwargs = dict(spec.kwargs)

            if spec.step_id == "Yosys.Synthesis":
                verilog_files = list_verilog_paths(work_dir)
                if not verilog_files:
                    raise RuntimeError(
                        "No Verilog sources in the run workdir. "
                        "Create a new run and upload .v / .sv files."
                    )
                require_top_module_in_workdir(self.run.design_name, work_dir)
                kwargs["VERILOG_FILES"] = [str(p) for p in verilog_files]
                kwargs["state_in"] = State()
            else:
                if self._state is None:
                    raise RuntimeError(
                        "No prior state available. Run Synthesis first or use Run All."
                    )
                kwargs["state_in"] = self._state

            from librelane.common.misc import slugify

            step_dir = work_dir / f"{step_row.order + 1}-{slugify(spec.step_id)}"

            with redirect_stdout(log_capture), redirect_stderr(log_capture):
                instance = step_cls(**kwargs)
                instance.start(step_dir=str(step_dir))
                self._state = instance.state_out
                self._save_state()

            output = format_step_output(instance, work_dir=work_dir)
            step_row.status = FlowStepResult.Status.DONE
            step_row.output = output
            step_row.summary = format_step_summary_text(output)
            step_row.log = log_capture.getvalue()
            measure_and_save_run_sizes(self.run)
        except StorageLimitError as exc:
            step_row.status = FlowStepResult.Status.FAILED
            step_row.log = log_capture.getvalue() + "\n" + str(exc)
            step_row.summary = str(exc)
            step_row.output = {"error": str(exc)}
            self.run.status = FlowRun.Status.FAILED
            self.run.error_message = str(exc)
            self.run.save(update_fields=["status", "error_message", "updated_at"])
            raise
        except Exception as exc:
            step_row.status = FlowStepResult.Status.FAILED
            step_row.log = log_capture.getvalue() + "\n" + traceback.format_exc()
            step_row.summary = str(exc)
            step_row.output = {"error": str(exc)}
            self.run.status = FlowRun.Status.FAILED
            self.run.error_message = str(exc)
            self.run.save(update_fields=["status", "error_message", "updated_at"])
            raise
        finally:
            step_row.finished_at = dj_timezone.now()
            self._persist_step_row(step_row)

    def _persist_step_row(self, step_row: FlowStepResult) -> None:
        """Save step result; never leave status stuck at running due to output encoding."""
        try:
            step_row.save()
            return
        except Exception:
            pass
        # Last resort: persist status/log without the structured payload.
        step_row.output = {}
        try:
            step_row.save(
                update_fields=[
                    "status",
                    "summary",
                    "log",
                    "output",
                    "started_at",
                    "finished_at",
                ]
            )
        except Exception:
            FlowStepResult.objects.filter(pk=step_row.pk).update(
                status=step_row.status,
                summary=(step_row.summary or "")[:2000],
                log=(step_row.log or "")[-50000:],
                output={},
                finished_at=step_row.finished_at,
            )

    def _seed_step_rows(self) -> None:
        if self.run.steps.exists():
            return
        FlowStepResult.objects.bulk_create(
            [
                FlowStepResult(
                    run=self.run,
                    order=i,
                    step_id=spec.step_id,
                    title=spec.title,
                )
                for i, spec in enumerate(NOTEBOOK_STEPS)
            ]
        )

    def _save_state(self) -> None:
        if self._state is None or self._state_file is None:
            return
        try:
            with open(self._state_file, "wb") as f:
                pickle.dump(self._state, f)
        except Exception:
            pass

    def _load_state(self):
        path = Path(self.run.work_dir) / "librelane_state.pkl" if self.run.work_dir else None
        if path and path.is_file():
            try:
                with open(path, "rb") as f:
                    return pickle.load(f)
            except Exception:
                return None
        return None

    @staticmethod
    def _version() -> str:
        import librelane

        return librelane.__version__
