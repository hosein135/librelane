"""Run LibreLane steps sequentially, matching notebook.ipynb."""

from __future__ import annotations

import io
import shutil
import traceback
from contextlib import redirect_stderr, redirect_stdout
import pickle
from pathlib import Path

from django.conf import settings
from django.utils import timezone as dj_timezone

from flow.models import FlowRun, FlowStepResult
from flow.services.setup import configure_interactive, pdk_is_ready
from flow.services.step_output import format_step_output, format_step_summary_text
from flow.steps import NOTEBOOK_STEPS, StepSpec


class FlowRunner:
    def __init__(self, run: FlowRun) -> None:
        self.run = run
        self.work_dir = Path(run.work_dir) if run.work_dir else self._prepare_work_dir()
        self._state_file = self.work_dir / "librelane_state.pkl"
        self._state = self._load_state()

    def _prepare_work_dir(self) -> Path:
        base = settings.RUNS_DIR / f"run_{self.run.pk}"
        base.mkdir(parents=True, exist_ok=True)
        verilog_src = settings.DESIGNS_DIR / f"{self.run.design_name}.v"
        verilog_dst = base / f"{self.run.design_name}.v"
        if verilog_src.is_file():
            shutil.copy2(verilog_src, verilog_dst)
        self.run.work_dir = str(base)
        self.run.save(update_fields=["work_dir", "updated_at"])
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
            from flow.services.setup import check_tkinter

            self._save_setup_progress(log, f"LibreLane {self._version()}")
            self._save_setup_progress(log, "Checking environment…")
            check_tkinter()
            if pdk_is_ready(self.run.pdk_root, self.run.pdk_family):
                self._save_setup_progress(
                    log,
                    f"PDK «{self.run.pdk_family}» already enabled under {self.run.pdk_root}.",
                )
            else:
                self._save_setup_progress(
                    log,
                    "PDK is not ready yet. Run ./run.sh (or ./dev_run.sh) once and wait for "
                    "“PDK ready” in the terminal before using Setup PDK here.",
                )
                raise RuntimeError(
                    "PDK not downloaded. Run ./run.sh and wait for the first-startup PDK "
                    "download (~1 GB) to finish in the terminal."
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

    def run_all(self) -> None:
        if self.run.status not in (FlowRun.Status.READY, FlowRun.Status.RUNNING):
            self.setup()

        self.run.status = FlowRun.Status.RUNNING
        self.run.save(update_fields=["status", "updated_at"])

        for index, spec in enumerate(NOTEBOOK_STEPS):
            step_row = self.run.steps.get(order=index)
            if step_row.status == FlowStepResult.Status.DONE:
                if self._state is None:
                    self._state = self._load_state()
                continue
            self.run.current_step_index = index
            self.run.save(update_fields=["current_step_index", "updated_at"])
            self._run_single(step_row, spec)

        self.run.status = FlowRun.Status.COMPLETED
        self.run.save(update_fields=["status", "updated_at"])

    def run_step(self, order: int) -> None:
        if self.run.status == FlowRun.Status.PENDING:
            self.setup()

        spec = NOTEBOOK_STEPS[order]
        step_row = self.run.steps.get(order=order)
        self.run.status = FlowRun.Status.RUNNING
        self.run.current_step_index = order
        self.run.save(update_fields=["status", "current_step_index", "updated_at"])
        self._run_single(step_row, spec)

        if all(
            s.status in (FlowStepResult.Status.DONE, FlowStepResult.Status.SKIPPED)
            for s in self.run.steps.all()
        ):
            self.run.status = FlowRun.Status.COMPLETED
        else:
            self.run.status = FlowRun.Status.READY
        self.run.save(update_fields=["status", "updated_at"])

    def _run_single(self, step_row: FlowStepResult, spec: StepSpec) -> None:
        from librelane.state import State
        from librelane.steps import Step

        step_row.status = FlowStepResult.Status.RUNNING
        step_row.started_at = dj_timezone.now()
        step_row.log = ""
        step_row.save()

        log_capture = io.StringIO()
        try:
            step_cls = Step.factory.get(spec.step_id)
            kwargs = dict(spec.kwargs)

            if spec.step_id == "Yosys.Synthesis":
                verilog = self.work_dir / f"{self.run.design_name}.v"
                kwargs["VERILOG_FILES"] = [str(verilog)]
                kwargs["state_in"] = State()
            else:
                if self._state is None:
                    raise RuntimeError(
                        "No prior state available. Run Synthesis first or use Run All."
                    )
                kwargs["state_in"] = self._state

            from librelane.common.misc import slugify

            step_dir = self.work_dir / f"{step_row.order + 1}-{slugify(spec.step_id)}"

            with redirect_stdout(log_capture), redirect_stderr(log_capture):
                instance = step_cls(**kwargs)
                instance.start(step_dir=str(step_dir))
                self._state = instance.state_out
                self._save_state()

            output = format_step_output(instance, work_dir=self.work_dir)
            step_row.status = FlowStepResult.Status.DONE
            step_row.output = output
            step_row.summary = format_step_summary_text(output)
            step_row.log = log_capture.getvalue()
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
            step_row.save()

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
        if self._state is None:
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
