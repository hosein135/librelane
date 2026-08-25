"""Cookie session auth (username + session_nonce), matching jadex_django."""

from __future__ import annotations

import json
import secrets

from django.http import HttpRequest, HttpResponse, JsonResponse

from flow.models import User

COOKIE_MAX_AGE_SESSION = 3600


def get_cookie(request: HttpRequest, name: str) -> str | None:
    return request.COOKIES.get(name)


def set_cookie(response: HttpResponse, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        httponly=True,
        samesite="Lax",
    )


def clear_session_cookies(response: HttpResponse) -> None:
    for name in ("username", "session_nonce"):
        set_cookie(response, name, "", -1)


def generate_session_nonce() -> str:
    return secrets.token_hex(16)


def is_session_nonce_active(request: HttpRequest, username: str) -> bool:
    session_nonce = get_cookie(request, "session_nonce")
    if not session_nonce:
        return False
    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return False
    current = user.session_nonce or ""
    return bool(current) and current == session_nonce


def prefers_json_auth_response(request: HttpRequest) -> bool:
    xw = request.headers.get("X-Requested-With", "")
    if xw.lower() == "xmlhttprequest":
        return True
    mode = request.headers.get("Sec-Fetch-Mode", "")
    if mode and mode != "navigate":
        return True
    accept = request.headers.get("Accept", "")
    has_json = "application/json" in accept
    has_html = "text/html" in accept
    return has_json and not has_html


def expects_html(request: HttpRequest) -> bool:
    """True only for real browser document navigations — never for JSON fetch()."""
    ct = (request.content_type or "").lower()
    if "application/json" in ct:
        return False
    accept = request.headers.get("Accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return False
    if prefers_json_auth_response(request):
        return False
    return "text/html" in accept


SESSION_END_REASONS = frozenset({"session_invalidated", "expired", "logged_out"})

SESSION_REASON_MESSAGES = {
    "session_invalidated": (
        "You were signed out because you signed in from another browser or device. "
        "Only one sign-in can be active at a time."
    ),
    "expired": "Your session expired. Sign in again to continue.",
    "logged_out": "You signed out of this account.",
    "required": "Not signed in.",
    "bad_creds": "Invalid username or password.",
    "signup_ok": "Account created. Sign in to continue.",
}


def normalize_session_end_reason(reason: str | None) -> str:
    if reason in SESSION_END_REASONS:
        return reason
    return "session_invalidated"


def session_end_path(reason: str) -> str:
    return f"/session/end?reason={normalize_session_end_reason(reason)}"


def classify_invalid_session(request: HttpRequest) -> str:
    username = get_cookie(request, "username")
    nonce_cookie = get_cookie(request, "session_nonce")
    if not username:
        return "expired"
    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return "expired"
    current = user.session_nonce or ""
    if not nonce_cookie:
        return "expired"
    if not current:
        return "logged_out"
    if current != nonce_cookie:
        return "session_invalidated"
    return "expired"


def json_session_unauthorized(reason: str) -> JsonResponse:
    normalized = reason if reason in SESSION_REASON_MESSAGES else "expired"
    redirect_path = (
        "/login?reason=required" if normalized == "required" else session_end_path(normalized)
    )
    return JsonResponse(
        {"error": SESSION_REASON_MESSAGES[normalized], "redirect": redirect_path},
        status=401,
    )


def require_signed_in_username(request: HttpRequest) -> tuple[str | None, HttpResponse | None]:
    username = get_cookie(request, "username")
    if not username:
        if prefers_json_auth_response(request):
            return None, json_session_unauthorized("expired")
        response = HttpResponse(status=302)
        response["Location"] = "/login?reason=required"
        return None, response

    if not is_session_nonce_active(request, username):
        reason = classify_invalid_session(request)
        if prefers_json_auth_response(request):
            return None, json_session_unauthorized(reason)
        response = HttpResponse(status=302)
        response["Location"] = session_end_path(reason)
        return None, response

    return username, None


def get_user(username: str) -> User | None:
    try:
        return User.objects.get(username=username)
    except User.DoesNotExist:
        return None


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def email_in_use(email: str, *, exclude_user_id: int | None = None) -> bool:
    normalized = normalize_email(email)
    if not normalized:
        return False
    qs = User.objects.filter(email__iexact=normalized)
    if exclude_user_id is not None:
        qs = qs.exclude(id=exclude_user_id)
    return qs.exists()


def parse_auth_body(request: HttpRequest) -> dict | None:
    ct = (request.content_type or "").lower()
    if "application/json" in ct:
        try:
            data = json.loads(request.body)
            return {
                "username": data.get("username", ""),
                "password": data.get("password", ""),
                "email": data.get("email"),
                "first_name": data.get("first_name", ""),
                "last_name": data.get("last_name", ""),
            }
        except Exception:
            return None
    if request.method == "POST":
        return {
            "username": request.POST.get("username", ""),
            "password": request.POST.get("password", ""),
            "email": request.POST.get("email"),
            "first_name": request.POST.get("first_name", ""),
            "last_name": request.POST.get("last_name", ""),
        }
    return None
