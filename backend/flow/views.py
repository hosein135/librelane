from __future__ import annotations

import io
import json
import shutil
import threading
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from flow.models import FlowRun, FlowStepResult
from flow.services.downloads import (
    build_preview_svg,
    build_step_zip,
    preview_source_download_filename,
    preview_source_file,
    preview_source_info,
    step_can_download_preview_source,
    step_can_download_svg,
    step_can_download_zip,
    svg_download_filename,
    zip_download_filename,
)
from flow.services.runner import FlowRunner
from flow.services.setup import librelane_version
from flow.services.step_output import output_has_content
from flow.steps import NOTEBOOK_STEPS

_flow_threads: dict[int, threading.Thread] = {}


def _is_active(run_id: int) -> bool:
    thread = _flow_threads.get(run_id)
    return thread is not None and thread.is_alive()


def _clear_stale_running(run: FlowRun) -> None:
    if _is_active(run.pk):
        return

    if run.steps.filter(status=FlowStepResult.Status.RUNNING).exists():
        run.steps.filter(status=FlowStepResult.Status.RUNNING).update(
            status=FlowStepResult.Status.PENDING
        )

    if run.status not in (FlowRun.Status.RUNNING, FlowRun.Status.SETTING_UP):
        return

    if run.status == FlowRun.Status.SETTING_UP:
        run.status = FlowRun.Status.FAILED
        if not run.error_message:
            run.error_message = (
                "Setup timed out. Download the PDK on first ./run.sh startup "
                "(wait for “PDK ready” in the terminal), then click Setup PDK again."
            )
        run.save(update_fields=["status", "error_message", "updated_at"])
        return

    steps = list(run.steps.all())
    if steps and all(
        s.status in (FlowStepResult.Status.DONE, FlowStepResult.Status.SKIPPED)
        for s in steps
    ):
        run.status = FlowRun.Status.COMPLETED
    else:
        run.status = FlowRun.Status.READY
    run.save(update_fields=["status", "updated_at"])


def _redirect_watch(run_id: int, step: int | None = None) -> HttpResponse:
    url = reverse("run_detail", args=[run_id]) + "?watch=1"
    if step is not None:
        url += f"#step-{step}"
    return redirect(url)


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _remove_run_artifacts(run: FlowRun) -> None:
    runs_root = Path(settings.RUNS_DIR).resolve()
    data_root = Path(settings.DATA_DIR).resolve()
    candidate_paths: list[Path] = []

    if run.work_dir:
        candidate_paths.append(Path(run.work_dir))

    candidate_paths.append(runs_root / f"run_{run.pk}")

    for step in run.steps.all():
        step_dir = (step.output or {}).get("step_dir")
        if step_dir:
            candidate_paths.append(Path(step_dir))

    seen: set[Path] = set()
    for path in candidate_paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.exists():
            continue
        if not (
            _path_within(resolved, runs_root) or _path_within(resolved, data_root)
        ):
            continue
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()


def _start_background(
    run_id: int,
    *,
    run_all: bool,
    step_order: int | None = None,
    target: str = "flow",
) -> None:
    def worker() -> None:
        run = FlowRun.objects.get(pk=run_id)
        runner = FlowRunner(run)
        try:
            if target == "setup":
                runner.setup()
            elif run_all:
                runner.run_all()
            elif step_order is not None:
                runner.run_step(step_order)
        except Exception:
            pass
        finally:
            _flow_threads.pop(run_id, None)

    thread = threading.Thread(
        target=worker, daemon=True, name=f"{target}-{run_id}"
    )
    _flow_threads[run_id] = thread
    thread.start()


@require_http_methods(["GET"])
def home(request: HttpRequest) -> HttpResponse:
    runs = FlowRun.objects.order_by("-created_at")[:20]
    try:
        version = librelane_version()
    except Exception:
        version = "unknown (enter nix develop first)"
    return render(
        request,
        "flow/home.html",
        {
            "runs": runs,
            "librelane_version": version,
            "default_pdk": settings.LIBRELANE_PDK,
            "default_pdk_family": settings.LIBRELANE_PDK_FAMILY,
            "step_catalog": NOTEBOOK_STEPS,
        },
    )


@require_POST
def create_run(request: HttpRequest) -> HttpResponse:
    run = FlowRun.objects.create(
        design_name=request.POST.get("design_name", settings.LIBRELANE_DESIGN_NAME),
        pdk=request.POST.get("pdk", settings.LIBRELANE_PDK),
        pdk_family=request.POST.get("pdk_family", settings.LIBRELANE_PDK_FAMILY),
        pdk_root=request.POST.get("pdk_root", settings.PDK_ROOT),
        clock_period=float(request.POST.get("clock_period", "10")),
    )
    return redirect("run_detail", run_id=run.pk)


@require_http_methods(["GET"])
def run_detail(request: HttpRequest, run_id: int) -> HttpResponse:
    run = get_object_or_404(FlowRun, pk=run_id)
    _clear_stale_running(run)
    run.refresh_from_db()

    steps = list(run.steps.all())
    if not steps:
        for i, spec in enumerate(NOTEBOOK_STEPS):
            FlowStepResult.objects.create(
                run=run,
                order=i,
                step_id=spec.step_id,
                title=spec.title,
            )
        steps = list(run.steps.all())

    verilog_path = settings.DESIGNS_DIR / f"{run.design_name}.v"
    verilog_source = ""
    if verilog_path.is_file():
        verilog_source = verilog_path.read_text(encoding="utf-8")

    steps_info = []
    for s in steps:
        src = preview_source_info(run, s)
        steps_info.append(
            {
                "step": s,
                "description": NOTEBOOK_STEPS[s.order].description,
                "output_json": json.dumps(s.output or {}),
                "has_output": output_has_content(s.output),
                "can_download_zip": step_can_download_zip(run, s),
                "can_download_svg": step_can_download_svg(run, s),
                "can_download_preview_source": step_can_download_preview_source(run, s),
                "preview_source_name": src.get("name", "") if src else "",
            }
        )

    return render(
        request,
        "flow/run_detail.html",
        {
            "run": run,
            "steps_info": steps_info,
            "verilog_source": verilog_source,
            "is_running": _is_active(run.pk),
            "watch": request.GET.get("watch") == "1",
        },
    )


@require_POST
def run_setup(request: HttpRequest, run_id: int) -> HttpResponse:
    get_object_or_404(FlowRun, pk=run_id)
    if not _is_active(run_id):
        _start_background(run_id, run_all=False, target="setup")
    return _redirect_watch(run_id)


@require_POST
def run_all(request: HttpRequest, run_id: int) -> HttpResponse:
    get_object_or_404(FlowRun, pk=run_id)
    if not _is_active(run_id):
        _start_background(run_id, run_all=True)
    return _redirect_watch(run_id)


@require_POST
def run_step(request: HttpRequest, run_id: int, order: int) -> HttpResponse:
    get_object_or_404(FlowRun, pk=run_id)
    if not _is_active(run_id):
        _start_background(run_id, run_all=False, step_order=order)
    return _redirect_watch(run_id, step=order)


@require_http_methods(["GET"])
def run_status(request: HttpRequest, run_id: int) -> JsonResponse:
    run = get_object_or_404(FlowRun, pk=run_id)
    _clear_stale_running(run)
    run.refresh_from_db()

    steps = [
        {
            "order": s.order,
            "step_id": s.step_id,
            "title": s.title,
            "status": s.status,
            "log": s.log,
            "summary": s.summary,
            "output": s.output or {},
        }
        for s in run.steps.all()
    ]
    return JsonResponse(
        {
            "id": run.pk,
            "status": run.status,
            "current_step_index": run.current_step_index,
            "error_message": run.error_message,
            "setup_log": run.setup_log,
            "is_running": _is_active(run.pk),
            "steps": steps,
        }
    )


def _get_run_step(run_id: int, order: int) -> tuple[FlowRun, FlowStepResult]:
    run = get_object_or_404(FlowRun, pk=run_id)
    step = get_object_or_404(FlowStepResult, run=run, order=order)
    return run, step


@require_http_methods(["GET"])
def download_step_zip(request: HttpRequest, run_id: int, order: int) -> FileResponse:
    run, step = _get_run_step(run_id, order)
    data = build_step_zip(run, step)
    response = FileResponse(
        io.BytesIO(data),
        as_attachment=True,
        filename=zip_download_filename(run, step),
        content_type="application/zip",
    )
    response["Content-Length"] = len(data)
    return response


@require_http_methods(["GET"])
def download_step_preview_svg(
    request: HttpRequest, run_id: int, order: int
) -> FileResponse:
    run, step = _get_run_step(run_id, order)
    try:
        data = build_preview_svg(run, step)
    except Http404:
        raise
    as_attachment = request.GET.get("download") == "1"
    response = FileResponse(
        io.BytesIO(data),
        as_attachment=as_attachment,
        filename=svg_download_filename(run, step),
        content_type="image/svg+xml",
    )
    response["Content-Length"] = len(data)
    if not as_attachment:
        response["Cache-Control"] = "private, max-age=60"
    return response


@require_http_methods(["GET"])
def download_step_preview_source(
    request: HttpRequest, run_id: int, order: int
) -> FileResponse:
    run, step = _get_run_step(run_id, order)
    path = preview_source_file(run, step)
    if path is None:
        raise Http404("No layout source file for this step preview.")

    import mimetypes

    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    response = FileResponse(
        open(path, "rb"),
        as_attachment=True,
        filename=preview_source_download_filename(run, step),
        content_type=content_type,
    )
    return response


@require_POST
def delete_run(request: HttpRequest, run_id: int) -> HttpResponse:
    run = get_object_or_404(FlowRun, pk=run_id)

    if _is_active(run_id):
        return redirect("run_detail", run_id=run_id)

    _flow_threads.pop(run_id, None)
    _remove_run_artifacts(run)
    run.delete()
    return redirect("home")
