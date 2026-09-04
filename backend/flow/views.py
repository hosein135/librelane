from __future__ import annotations

import io
import json
import threading
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone as dj_timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from flow.auth_helpers import (
    COOKIE_MAX_AGE_SESSION,
    classify_invalid_session,
    clear_session_cookies,
    email_in_use,
    expects_html,
    generate_session_nonce,
    get_cookie,
    get_user,
    json_session_unauthorized,
    normalize_email,
    normalize_session_end_reason,
    parse_auth_body,
    require_signed_in_username,
    set_cookie,
)
from flow.models import FlowRun, FlowStepResult, User
from flow.pdk_catalog import SUPPORTED_PDK_VARIANT_LIST, family_for_variant
from flow.services.downloads import (
    build_preview_svg,
    build_run_zip,
    build_step_zip,
    preview_source_db_file,
    preview_source_download_filename,
    preview_source_file,
    preview_source_info,
    run_can_download_all_files,
    run_zip_download_filename,
    step_can_download_preview_source,
    step_can_download_zip,
    svg_preview_filename,
    zip_download_filename,
)
from flow.services.fabpack.integrate import load_foundry_gds
from flow.services.fabpack.jobs import (
    fab_job_is_running,
    fab_status,
    start_fab_job,
    stop_fab_job,
    user_has_fab_job,
)
from flow.services.fabpack.targets import get_target
from flow.services.fabrication import (
    build_fabrication_zip,
    fabrication_targets_for_run,
    fabrication_zip_filename,
    run_can_export_fabrication,
)
from flow.services.runner import FlowRunner, ensure_step_rows
from flow.services.setup import librelane_version
from flow.services.step_output import output_has_content
from flow.services.storage import (
    StorageLimitError,
    assert_can_start_run,
    prune_expired_workdirs,
    storage_snapshot,
)
from flow.services.verilog import (
    modules_in_workdir,
    read_sources_text,
    require_top_module_in_workdir,
    store_uploaded_verilog,
)
from flow.services.workdir import create_run_workdir, measure_and_save_run_sizes, remove_run_temp
from flow.steps import NOTEBOOK_STEPS

_flow_threads: dict[int, threading.Thread] = {}
_ACTIVE_STATUSES = (
    FlowRun.Status.SETTING_UP,
    FlowRun.Status.RUNNING,
)


def _is_active(run_id: int) -> bool:
    thread = _flow_threads.get(run_id)
    return thread is not None and thread.is_alive()


def _run_appears_active(run: FlowRun) -> bool:
    """True while a worker is live or the run was recently progressing."""
    if _is_active(run.pk):
        return True
    if run.status not in _ACTIVE_STATUSES:
        return False
    now = dj_timezone.now()
    last_touch = run.updated_at or run.created_at
    if last_touch and (now - last_touch) < timedelta(hours=12):
        return True
    running_step = (
        run.steps.filter(status=FlowStepResult.Status.RUNNING)
        .order_by("-started_at")
        .first()
    )
    return bool(
        running_step is not None
        and running_step.started_at is not None
        and (now - running_step.started_at) < timedelta(hours=24)
    )


def _user_has_active_run(user: User, *, exclude_run_id: int | None = None) -> bool:
    qs = FlowRun.objects.filter(owner_user=user, status__in=_ACTIVE_STATUSES)
    if exclude_run_id is not None:
        qs = qs.exclude(pk=exclude_run_id)
    for run in qs:
        if _is_active(run.pk) or run.status in _ACTIVE_STATUSES:
            return True
    # Also treat in-memory threads for this user's runs.
    for run_id, thread in list(_flow_threads.items()):
        if not thread.is_alive():
            continue
        if exclude_run_id is not None and run_id == exclude_run_id:
            continue
        try:
            other = FlowRun.objects.get(pk=run_id)
        except FlowRun.DoesNotExist:
            continue
        if other.owner_user_id == user.id:
            return True
    if user_has_fab_job(user.id):
        return True
    return False


def _clear_stale_running(run: FlowRun) -> None:
    """
    Heal runs left marked running after a worker truly died.

    Never clear a recently-updated active run: session expiry / navigating away
    must not stop (or appear to stop) an in-progress flow. LibreLane steps can
    run for hours, so use a long grace window based on run/step activity.
    """
    if _is_active(run.pk):
        return

    if run.status in _ACTIVE_STATUSES:
        now = dj_timezone.now()
        last_touch = run.updated_at or run.created_at
        if last_touch and (now - last_touch) < timedelta(hours=12):
            return
        running_step = (
            run.steps.filter(status=FlowStepResult.Status.RUNNING)
            .order_by("-started_at")
            .first()
        )
        if (
            running_step is not None
            and running_step.started_at is not None
            and (now - running_step.started_at) < timedelta(hours=24)
        ):
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
                "Setup timed out. Download PDKs on first ./run.sh startup "
                "(wait for “PDKs ready” in the terminal), then click Setup PDK again."
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


def _json_body(request: HttpRequest) -> dict:
    content_type = (request.META.get("CONTENT_TYPE") or "").lower()
    if "multipart/form-data" in content_type:
        return {}
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_request_data(request: HttpRequest) -> dict:
    data = _json_body(request)
    if data:
        return data
    return {k: request.POST.get(k) for k in request.POST.keys()}


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

    # Background workers are independent of the browser session: logging out or
    # letting the session expire must not cancel them. daemon=True only ties
    # lifetime to the Django process (not to cookies / HTTP requests).
    thread = threading.Thread(
        target=worker, daemon=True, name=f"{target}-{run_id}"
    )
    _flow_threads[run_id] = thread
    thread.start()


def _allocate_unique_run_name(user: User, base: str) -> str:
    """Use top-module name; append _2, _3, … if that name already exists for the user."""
    base = (base or "run").strip()[:240] or "run"
    existing = set(
        FlowRun.objects.filter(owner_user=user).values_list("name", flat=True)
    )
    if base not in existing:
        return base
    n = 2
    while True:
        candidate = f"{base}_{n}"
        if candidate not in existing:
            return candidate[:256]
        n += 1


def _run_progress(run: FlowRun) -> dict[str, int]:
    steps = list(run.steps.all())
    total = len(steps) or len(NOTEBOOK_STEPS)
    done = sum(
        1
        for s in steps
        if s.status
        in (FlowStepResult.Status.DONE, FlowStepResult.Status.SKIPPED)
    )
    pct = int(round((100 * done) / total)) if total else 0
    return {
        "steps_total": total,
        "steps_done": done,
        "progress_pct": pct,
    }


def _run_to_dict(run: FlowRun, *, include_steps: bool = False) -> dict:
    progress = _run_progress(run)
    data = {
        "id": run.pk,
        "name": run.name,
        "status": run.status,
        "design_name": run.design_name,
        "top_module_name": run.design_name,
        "pdk": run.pdk,
        "pdk_family": run.pdk_family,
        "pdk_root": run.pdk_root,
        "clock_period": run.clock_period,
        "work_dir": run.work_dir,
        "temp_folder_name": run.temp_folder_name,
        "current_step_index": run.current_step_index,
        "error_message": run.error_message,
        "setup_log": run.setup_log,
        "artifacts_stored": run.artifacts_stored,
        "disk_bytes": int(getattr(run, "disk_bytes", 0) or 0),
        "db_bytes": int(getattr(run, "db_bytes", 0) or 0),
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "is_running": _run_appears_active(run),
        "owner_username": run.owner_user.username if run.owner_user_id else None,
        "can_download_all_files": run_can_download_all_files(run),
        "can_export_fabrication": run_can_export_fabrication(run),
        "fabrication_targets": fabrication_targets_for_run(run),
        "steps_total": progress["steps_total"],
        "steps_done": progress["steps_done"],
        "progress_pct": progress["progress_pct"],
    }
    if include_steps:
        data["steps"] = [
            {
                "order": s.order,
                "step_id": s.step_id,
                "title": s.title,
                "status": s.status,
                "log": s.log,
                "summary": s.summary,
                "output": s.output or {},
                "has_output": output_has_content(s.output),
                "can_download_zip": step_can_download_zip(run, s),
                "can_download_preview_source": step_can_download_preview_source(run, s),
                "preview_source_name": (
                    (preview_source_info(run, s) or {}).get("name")
                    or (getattr(preview_source_db_file(run, s), "relative_path", "") or "").split("/")[-1]
                ),
                "description": NOTEBOOK_STEPS[s.order].description
                if s.order < len(NOTEBOOK_STEPS)
                else "",
            }
            for s in run.steps.all()
        ]
    return data


def _owned_run(request: HttpRequest, run_id: int) -> tuple[FlowRun | None, HttpResponse | None]:
    username, err = require_signed_in_username(request)
    if err:
        return None, err
    user = get_user(username or "")
    if user is None:
        return None, json_session_unauthorized("expired")
    run = get_object_or_404(FlowRun, pk=run_id, owner_user=user)
    return run, None


def redirect_to_next(request: HttpRequest, path: str = "/") -> HttpResponse:
    origin = getattr(settings, "NEXT_ORIGIN", "http://127.0.0.1:3000").rstrip("/")
    response = HttpResponse(status=302)
    response["Location"] = f"{origin}{path}"
    return response


@require_http_methods(["GET", "POST"])
def signup(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        return redirect_to_next(request, "/signup")
    body = parse_auth_body(request)
    if not body:
        return JsonResponse(
            {"error": "Invalid request body. Expected JSON with the required fields."},
            status=400,
        )
    if not body["username"] or not body["password"] or not body.get("email"):
        return JsonResponse(
            {"error": "All fields are required: username, email, and password."},
            status=400,
        )
    email = normalize_email(body.get("email"))
    if not email:
        return JsonResponse({"error": "Email is required"}, status=400)
    if email_in_use(email):
        return JsonResponse({"error": "An account with this email already exists."}, status=409)
    if User.objects.filter(username=body["username"]).exists():
        return JsonResponse(
            {"error": "That username is already taken. Choose a different username."},
            status=409,
        )
    try:
        User.objects.create(
            username=body["username"],
            password=body["password"],
            email=email,
            first_name=str(body.get("first_name") or "")[:100],
            last_name=str(body.get("last_name") or "")[:100],
        )
    except Exception:
        return JsonResponse(
            {"error": "Could not create the account. The username may already exist."},
            status=409,
        )
    if expects_html(request):
        return redirect_to_next(request, "/login?reason=signup_ok")
    return JsonResponse({"status": "ok"}, status=201)


@require_http_methods(["GET", "POST"])
def login(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        return redirect_to_next(request, "/login")
    body = parse_auth_body(request)
    if not body:
        return JsonResponse(
            {"error": "Invalid request body. Expected JSON with the required fields."},
            status=400,
        )
    try:
        User.objects.get(username=body["username"], password=body["password"])
    except User.DoesNotExist:
        if expects_html(request):
            return redirect_to_next(request, "/login?reason=bad_creds")
        return JsonResponse(
            {"error": "Invalid username or password. Check your credentials and try again."},
            status=401,
        )
    nonce = generate_session_nonce()
    User.objects.filter(username=body["username"]).update(session_nonce=nonce)
    response = (
        redirect_to_next(request, "/")
        if expects_html(request)
        else JsonResponse({"status": "ok"})
    )
    clear_session_cookies(response)
    set_cookie(response, "username", body["username"], COOKIE_MAX_AGE_SESSION)
    set_cookie(response, "session_nonce", nonce, COOKIE_MAX_AGE_SESSION)
    return response


@require_GET
def logout(request: HttpRequest) -> HttpResponse:
    # Clear cookies / nonce only — never touch in-flight flow workers.
    username = get_cookie(request, "username")
    if username:
        User.objects.filter(username=username).update(session_nonce="")
    response = redirect_to_next(request, "/login?reason=logged_out")
    clear_session_cookies(response)
    return response


@require_GET
def session_end(request: HttpRequest) -> HttpResponse:
    # Session expiry is UI-only; background runs keep going.
    reason_param = request.GET.get("reason")
    if reason_param:
        reason = normalize_session_end_reason(reason_param)
    else:
        reason = classify_invalid_session(request)
    response = redirect_to_next(request, f"/login?reason={reason}")
    clear_session_cookies(response)
    return response


@require_http_methods(["GET", "POST"])
def api_session_ping(request: HttpRequest) -> JsonResponse:
    username, err = require_signed_in_username(request)
    if err:
        return err  # type: ignore[return-value]
    return JsonResponse({"status": "ok", "username": username})


@require_GET
def api_home(request: HttpRequest) -> JsonResponse:
    username, err = require_signed_in_username(request)
    if err:
        return err  # type: ignore[return-value]
    user = get_user(username or "")
    if user is None:
        return json_session_unauthorized("expired")

    runs = FlowRun.objects.filter(owner_user=user).order_by("-created_at")[:20]
    for run in runs:
        _clear_stale_running(run)
    try:
        version = librelane_version()
    except Exception:
        version = "unknown (enter nix develop first)"

    busy = _user_has_active_run(user)
    try:
        prune_expired_workdirs()
    except Exception:
        pass
    storage = storage_snapshot(user)
    return JsonResponse(
        {
            "username": username,
            "librelane_version": version,
            "default_pdk": settings.LIBRELANE_PDK,
            "pdk_variants": SUPPORTED_PDK_VARIANT_LIST,
            "storage": storage.as_dict(),
            "step_catalog": [
                {"step_id": s.step_id, "title": s.title, "description": s.description}
                for s in NOTEBOOK_STEPS
            ],
            "busy": busy,
            "runs": [
                _run_to_dict(r)
                for r in FlowRun.objects.filter(owner_user=user)
                .prefetch_related("steps")
                .order_by("-created_at")[:20]
            ],
        }
    )


@require_http_methods(["POST"])
def create_run(request: HttpRequest) -> HttpResponse:
    username, err = require_signed_in_username(request)
    if err:
        return err
    user = get_user(username or "")
    if user is None:
        return json_session_unauthorized("expired")

    if _user_has_active_run(user):
        # Clear stale markers from crashed workers, then re-check.
        for stale in FlowRun.objects.filter(owner_user=user, status__in=_ACTIVE_STATUSES):
            _clear_stale_running(stale)
        if _user_has_active_run(user):
            return JsonResponse(
                {
                    "error": (
                        "You already have a run in progress. "
                        "Finish or wait for it before starting a new one."
                    )
                },
                status=409,
            )

    data = _parse_request_data(request)
    top_module = str(
        data.get("top_module_name") or data.get("design_name") or ""
    ).strip()
    if not top_module:
        return JsonResponse(
            {"error": "Top module name is required. Analyze uploads and select a module."},
            status=400,
        )

    uploads: list[tuple[str, bytes]] = []
    for uploaded in request.FILES.getlist("verilog_files"):
        uploads.append((uploaded.name, uploaded.read()))
    if not uploads:
        # Also accept a single field name used by some clients.
        single = request.FILES.get("verilog_file")
        if single is not None:
            uploads.append((single.name, single.read()))
    if not uploads:
        return JsonResponse(
            {"error": "Upload at least one Verilog file (.v or .sv)."},
            status=400,
        )

    pdk = str(data.get("pdk") or settings.LIBRELANE_PDK).strip()
    try:
        pdk_family = family_for_variant(pdk)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    try:
        assert_can_start_run(user)
    except StorageLimitError as exc:
        return JsonResponse({"error": str(exc)}, status=507)

    run_name = _allocate_unique_run_name(user, top_module)
    run = FlowRun.objects.create(
        owner_user=user,
        name=run_name,
        design_name=top_module[:128],
        pdk=pdk[:64],
        pdk_family=pdk_family[:64],
        pdk_root=settings.PDK_ROOT,
        clock_period=float(data.get("clock_period") or 10),
    )

    try:
        base, folder_key = create_run_workdir(run)
        store_uploaded_verilog(base, uploads)
        require_top_module_in_workdir(top_module, base)
        run.work_dir = str(base)
        run.temp_folder_name = folder_key
        run.save(update_fields=["work_dir", "temp_folder_name", "updated_at"])
        measure_and_save_run_sizes(run)
    except ValueError as exc:
        remove_run_temp(run)
        run.delete()
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        remove_run_temp(run)
        run.delete()
        return JsonResponse(
            {"error": f"Could not store uploaded Verilog: {exc}"},
            status=500,
        )

    if expects_html(request):
        return redirect_to_next(request, f"/?id={run.pk}")
    return JsonResponse({"status": "ok", "run": _run_to_dict(run)}, status=201)


@require_GET
def run_detail(request: HttpRequest, run_id: int) -> JsonResponse:
    run, err = _owned_run(request, run_id)
    if err:
        return err  # type: ignore[return-value]
    assert run is not None
    _clear_stale_running(run)
    run.refresh_from_db()
    ensure_step_rows(run)

    work = Path(run.work_dir) if run.work_dir else None
    file_names, verilog_source = read_sources_text(work)

    payload = _run_to_dict(run, include_steps=True)
    payload["verilog_source"] = verilog_source
    payload["verilog_files"] = file_names
    payload["module_names"] = modules_in_workdir(work)
    return JsonResponse(payload)


def _reject_if_top_module_invalid(run: FlowRun) -> JsonResponse | None:
    work = Path(run.work_dir) if run.work_dir else None
    try:
        require_top_module_in_workdir(run.design_name, work)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return None


@require_POST
def run_setup(request: HttpRequest, run_id: int) -> HttpResponse:
    run, err = _owned_run(request, run_id)
    if err:
        return err
    assert run is not None
    bad = _reject_if_top_module_invalid(run)
    if bad:
        return bad
    user = run.owner_user
    if _user_has_active_run(user, exclude_run_id=run.pk) or _is_active(run.pk):
        return JsonResponse(
            {"error": "A run is already in progress. Wait until it finishes."},
            status=409,
        )
    _start_background(run.pk, run_all=False, target="setup")
    if expects_html(request):
        return redirect_to_next(request, f"/?id={run.pk}")
    return JsonResponse({"status": "ok", "run": _run_to_dict(run)})


@require_POST
def run_all(request: HttpRequest, run_id: int) -> HttpResponse:
    run, err = _owned_run(request, run_id)
    if err:
        return err
    assert run is not None
    if run.status == FlowRun.Status.COMPLETED:
        return JsonResponse(
            {"error": "This run is already completed."},
            status=409,
        )
    bad = _reject_if_top_module_invalid(run)
    if bad:
        return bad
    user = run.owner_user
    if _user_has_active_run(user, exclude_run_id=run.pk) or _is_active(run.pk):
        return JsonResponse(
            {"error": "A run is already in progress. Wait until it finishes."},
            status=409,
        )
    _start_background(run.pk, run_all=True)
    if expects_html(request):
        return redirect_to_next(request, f"/runs/{run.pk}?watch=1")
    return JsonResponse({"status": "ok", "run": _run_to_dict(run)})


@require_POST
def run_step(request: HttpRequest, run_id: int, order: int) -> HttpResponse:
    run, err = _owned_run(request, run_id)
    if err:
        return err
    assert run is not None
    if run.status == FlowRun.Status.COMPLETED:
        return JsonResponse(
            {"error": "This run is already completed."},
            status=409,
        )
    bad = _reject_if_top_module_invalid(run)
    if bad:
        return bad
    user = run.owner_user
    if _user_has_active_run(user, exclude_run_id=run.pk) or _is_active(run.pk):
        return JsonResponse(
            {"error": "A run is already in progress. Wait until it finishes."},
            status=409,
        )
    _start_background(run.pk, run_all=False, step_order=order)
    if expects_html(request):
        return redirect_to_next(
            request, f"/runs/{run.pk}?watch=1#step-{order}"
        )
    return JsonResponse({"status": "ok", "run": _run_to_dict(run)})


@require_GET
def run_status(request: HttpRequest, run_id: int) -> JsonResponse:
    run, err = _owned_run(request, run_id)
    if err:
        return err  # type: ignore[return-value]
    assert run is not None
    _clear_stale_running(run)
    run.refresh_from_db()
    return JsonResponse(_run_to_dict(run, include_steps=True))


def _get_run_step(run: FlowRun, order: int) -> FlowStepResult:
    return get_object_or_404(FlowStepResult, run=run, order=order)


@require_http_methods(["GET"])
def download_run_zip(request: HttpRequest, run_id: int) -> FileResponse:
    run, err = _owned_run(request, run_id)
    if err:
        return err  # type: ignore[return-value]
    assert run is not None
    if run.status != FlowRun.Status.COMPLETED:
        raise Http404("Download is only available for completed runs.")
    data = build_run_zip(run)
    response = FileResponse(
        io.BytesIO(data),
        as_attachment=True,
        filename=run_zip_download_filename(run),
        content_type="application/zip",
    )
    response["Content-Length"] = len(data)
    return response


@require_POST
def start_fabrication_build(
    request: HttpRequest, run_id: int, target_id: str
) -> JsonResponse:
    run, err = _owned_run(request, run_id)
    if err:
        return err  # type: ignore[return-value]
    assert run is not None
    if not run_can_export_fabrication(run):
        return JsonResponse(
            {"error": "Shuttle GDS build is available after the run completes."},
            status=409,
        )
    try:
        get_target(target_id, run.pdk)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if fab_job_is_running(run.pk, target_id):
        return JsonResponse(fab_status(run, target_id))
    user = run.owner_user
    if _user_has_active_run(user):
        return JsonResponse(
            {
                "error": (
                    "A run or fabrication GDS build is already in progress. "
                    "Wait until it finishes."
                )
            },
            status=409,
        )
    force = bool((_parse_request_data(request) or {}).get("force"))
    try:
        start_fab_job(run, target_id, force=force)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except StorageLimitError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse(fab_status(run, target_id))


@require_GET
def fabrication_build_status(
    request: HttpRequest, run_id: int, target_id: str
) -> JsonResponse:
    run, err = _owned_run(request, run_id)
    if err:
        return err  # type: ignore[return-value]
    assert run is not None
    try:
        get_target(target_id, run.pdk)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(fab_status(run, target_id))


@require_POST
def stop_fabrication_build(
    request: HttpRequest, run_id: int, target_id: str
) -> JsonResponse:
    """Stop a background shuttle GDS build and delete its generated files."""
    run, err = _owned_run(request, run_id)
    if err:
        return err  # type: ignore[return-value]
    assert run is not None
    try:
        get_target(target_id, run.pdk)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if not fab_job_is_running(run.pk, target_id):
        state = fab_status(run, target_id)
        if state.get("status") == "running":
            # Stale "running" on disk/DB with no live process — heal + cleanup.
            return JsonResponse(stop_fab_job(run, target_id))
        return JsonResponse(
            {"error": "No shuttle GDS build is running for this target.", "status": state},
            status=409,
        )
    return JsonResponse(stop_fab_job(run, target_id))


@require_http_methods(["GET"])
def download_fabrication_zip(
    request: HttpRequest, run_id: int, target_id: str
) -> HttpResponse:
    run, err = _owned_run(request, run_id)
    if err:
        return err  # type: ignore[return-value]
    assert run is not None
    if not run_can_export_fabrication(run):
        return JsonResponse(
            {
                "error": (
                    "Export to fabrication is available after the run completes "
                    "and output files are present."
                )
            },
            status=409,
        )
    try:
        get_target(target_id, run.pdk)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if fab_job_is_running(run.pk, target_id):
        return JsonResponse(
            {
                "error": "Shuttle top GDS is still building. Wait until it finishes.",
                "status": fab_status(run, target_id),
            },
            status=409,
        )
    if load_foundry_gds(run, target_id) is None:
        return JsonResponse(
            {
                "error": (
                    "Build the shuttle top GDS first (Export to Fabrication → "
                    "Build shuttle GDS). This is a chip-level LibreLane run and "
                    "usually takes 30–90 minutes; IHP is a short GDS rename."
                ),
                "status": fab_status(run, target_id),
            },
            status=409,
        )
    try:
        data = build_fabrication_zip(run, target_id)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Http404 as exc:
        return JsonResponse({"error": str(exc)}, status=404)
    response = FileResponse(
        io.BytesIO(data),
        as_attachment=True,
        filename=fabrication_zip_filename(run, target_id),
        content_type="application/zip",
    )
    response["Content-Length"] = len(data)
    return response


@require_http_methods(["GET"])
def download_step_zip(request: HttpRequest, run_id: int, order: int) -> FileResponse:
    run, err = _owned_run(request, run_id)
    if err:
        return err  # type: ignore[return-value]
    assert run is not None
    step = _get_run_step(run, order)
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
    run, err = _owned_run(request, run_id)
    if err:
        return err  # type: ignore[return-value]
    assert run is not None
    step = _get_run_step(run, order)
    try:
        data = build_preview_svg(run, step)
    except Http404:
        raise
    response = FileResponse(
        io.BytesIO(data),
        as_attachment=False,
        filename=svg_preview_filename(run, step),
        content_type="image/svg+xml",
    )
    response["Content-Length"] = len(data)
    response["Cache-Control"] = "private, max-age=60"
    return response


@require_http_methods(["GET"])
def download_step_preview_source(
    request: HttpRequest, run_id: int, order: int
) -> FileResponse:
    run, err = _owned_run(request, run_id)
    if err:
        return err  # type: ignore[return-value]
    assert run is not None
    step = _get_run_step(run, order)
    path = preview_source_file(run, step)
    if path is not None:
        import mimetypes

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(
            open(path, "rb"),
            as_attachment=True,
            filename=preview_source_download_filename(run, step),
            content_type=content_type,
        )

    row = preview_source_db_file(run, step)
    if row is None:
        raise Http404("No layout source file for this step preview.")
    name = Path(row.relative_path).name
    return FileResponse(
        io.BytesIO(bytes(row.content)),
        as_attachment=True,
        filename=name or preview_source_download_filename(run, step),
        content_type=row.content_type or "application/octet-stream",
    )


@require_POST
def delete_run(request: HttpRequest, run_id: int) -> HttpResponse:
    run, err = _owned_run(request, run_id)
    if err:
        return err
    assert run is not None

    if _is_active(run_id) or fab_job_is_running(run_id):
        return JsonResponse(
            {"error": "Cannot delete a run while it is still executing."},
            status=409,
        )

    _flow_threads.pop(run_id, None)
    remove_run_temp(run)
    run.delete()
    if expects_html(request):
        return redirect_to_next(request, "/")
    return JsonResponse({"status": "ok"})


@require_GET
def root_redirect(request: HttpRequest) -> HttpResponse:
    return redirect_to_next(request, "/")
