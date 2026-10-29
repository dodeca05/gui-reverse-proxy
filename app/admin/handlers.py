"""관리자 웹 페이지 (서버 사이드 렌더링).

IP 주소로 직접 접속한 요청만 이 핸들러에 도달한다 (app.main.host_dispatch 참고).
모든 설정 화면은 로그인 세션이 필요하고, 상태를 바꾸는 요청은 POST + CSRF 토큰으로만 받는다.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import jinja2
from aiohttp import web
from multidict import MultiDictProxy

from ..context import LOGIN_LIMITER, REPOSITORY, ROUTE_SERVICE, SESSIONS, SETTINGS, TEMPLATES
from ..database.models import Route, RouteInput
from ..database.repository import DuplicateDomainError, RouteNotFoundError
from ..net import ip_in_networks, resolve_client_ip
from ..validation import (
    TargetPolicy,
    ValidationError,
    normalize_domain,
    normalize_service_name,
    parse_port,
)
from .auth import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    Session,
    new_token,
    same_origin,
    tokens_match,
    verify_credentials,
)

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
routes = web.RouteTableDef()
SESSION = web.RequestKey("admin_session", Session)


def setup_admin(app: web.Application) -> None:
    app[TEMPLATES] = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        autoescape=True,
    )
    app.router.add_routes(routes)
    app.router.add_static("/static", STATIC_DIR)


async def admin_gate(request: web.Request, handler: Handler) -> web.StreamResponse:
    """IP 직접 접속 요청의 진입점: 관리자 접근 IP 허용 목록을 확인하고 보안 헤더를 붙인다."""
    if request.path != "/healthz":
        settings = request.app[SETTINGS]
        client_ip = admin_client_ip(request)
        if not ip_in_networks(client_ip, settings.admin_allowed_networks):
            log.warning("admin access denied for %s (not in ADMIN_ALLOWED_IPS)", client_ip)
            return _secure(web.Response(status=403, text="403 Forbidden\n"))
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        _secure(exc)
        raise
    return _secure(response)


def _secure(response: web.StreamResponse) -> web.StreamResponse:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def admin_client_ip(request: web.Request) -> str:
    settings = request.app[SETTINGS]
    peer = request.remote or ""
    return (
        resolve_client_ip(peer, request.headers.getall("X-Forwarded-For", ()), settings.trusted_proxies)
        or peer
    )


# ---------------------------------------------------------------------------
# 공통 도우미
# ---------------------------------------------------------------------------


def render(request: web.Request, template: str, *, status: int = 200, **context: Any) -> web.Response:
    html = request.app[TEMPLATES].get_template(template).render(**context)
    return web.Response(text=html, status=status, content_type="text/html", charset="utf-8")


def current_session(request: web.Request) -> Session | None:
    return request.app[SESSIONS].load(request.cookies.get(SESSION_COOKIE))


def login_required(handler: Handler) -> Handler:
    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.StreamResponse:
        session = current_session(request)
        if session is None:
            if request.method in ("GET", "HEAD"):
                raise web.HTTPSeeOther("/login")
            raise web.HTTPUnauthorized(text="401 Unauthorized: login required\n")
        request[SESSION] = session
        return await handler(request)

    return wrapper


async def read_protected_form(request: web.Request, session: Session) -> MultiDictProxy[str]:
    """출처 확인 + CSRF 토큰 검증 후 폼 데이터를 반환한다."""
    if not same_origin(request):
        raise web.HTTPForbidden(text="403 Forbidden: cross-origin request\n")
    form = await request.post()
    if not tokens_match(session.csrf_token, _field(form, "csrf_token")):
        raise web.HTTPForbidden(text="403 Forbidden: invalid CSRF token\n")
    return form  # type: ignore[return-value]


def _field(form: MultiDictProxy[Any], name: str) -> str:
    value = form.get(name, "")
    return value if isinstance(value, str) else ""


def _route_id(request: web.Request) -> int:
    return int(request.match_info["route_id"])


def parse_route_form(
    form: MultiDictProxy[Any], policy: TargetPolicy
) -> tuple[RouteInput | None, dict[str, str], dict[str, Any]]:
    """폼 입력을 검증해 (RouteInput 또는 None, 필드별 오류, 화면에 다시 채울 값)을 반환한다."""
    values: dict[str, Any] = {
        "domain": _field(form, "domain").strip(),
        "target_service": _field(form, "target_service").strip(),
        "target_port": _field(form, "target_port").strip(),
        "enabled": "enabled" in form,
        "preserve_host": "preserve_host" in form,
    }
    errors: dict[str, str] = {}
    domain = service = ""
    port = 0
    try:
        domain = normalize_domain(values["domain"])
    except ValidationError as exc:
        errors["domain"] = str(exc)
    try:
        service = normalize_service_name(values["target_service"])
        policy.check(service)
    except ValidationError as exc:
        errors["target_service"] = str(exc)
    try:
        port = parse_port(values["target_port"])
    except ValidationError as exc:
        errors["target_port"] = str(exc)
    if errors:
        return None, errors, values
    data = RouteInput(
        domain=domain,
        target_service=service,
        target_port=port,
        enabled=values["enabled"],
        preserve_host=values["preserve_host"],
    )
    return data, {}, values


def _route_values(route: Route) -> dict[str, Any]:
    return {
        "domain": route.domain,
        "target_service": route.target_service,
        "target_port": str(route.target_port),
        "enabled": route.enabled,
        "preserve_host": route.preserve_host,
    }


NEW_ROUTE_DEFAULTS = {
    "domain": "",
    "target_service": "",
    "target_port": "",
    "enabled": True,
    "preserve_host": True,
}


async def _render_dashboard(
    request: web.Request,
    session: Session,
    *,
    status: int = 200,
    values: dict[str, Any] | None = None,
    errors: dict[str, str] | None = None,
) -> web.Response:
    service = request.app[ROUTE_SERVICE]
    route_list = await service.list_routes()
    return render(
        request,
        "dashboard.html",
        status=status,
        session=session,
        routes=[(r, _route_status(r, service.policy)) for r in route_list],
        active_count=sum(1 for r in route_list if _route_status(r, service.policy) == "active"),
        values=values or NEW_ROUTE_DEFAULTS,
        errors=errors or {},
        flashes=session.pop_flashes(),
    )


def _route_status(route: Route, policy: TargetPolicy) -> str:
    if not route.enabled:
        return "disabled"
    if not policy.allows(route.target_service):
        return "blocked"
    return "active"


# ---------------------------------------------------------------------------
# 로그인 / 로그아웃
# ---------------------------------------------------------------------------


def _login_page(
    request: web.Request, *, status: int = 200, error: str | None = None, username: str = ""
) -> web.Response:
    token = new_token()
    response = render(
        request, "login.html", status=status, csrf_token=token, error=error, username=username
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE, token, httponly=True, samesite="Strict", path="/", max_age=1800
    )
    return response


@routes.get("/login")
async def login_page(request: web.Request) -> web.StreamResponse:
    if current_session(request) is not None:
        raise web.HTTPSeeOther("/")
    return _login_page(request)


@routes.post("/login")
async def login(request: web.Request) -> web.StreamResponse:
    settings = request.app[SETTINGS]
    limiter = request.app[LOGIN_LIMITER]
    client_ip = admin_client_ip(request)

    retry_after = limiter.retry_after(client_ip)
    if retry_after:
        response = _login_page(
            request,
            status=429,
            error=f"로그인 시도가 너무 많습니다. {retry_after}초 후에 다시 시도하세요.",
        )
        response.headers["Retry-After"] = str(retry_after)
        return response

    if not same_origin(request):
        return _login_page(request, status=403, error="잘못된 요청 출처입니다.")
    form = await request.post()
    username = _field(form, "username")
    if not tokens_match(request.cookies.get(LOGIN_CSRF_COOKIE), _field(form, "csrf_token")):
        return _login_page(
            request, status=403, error="로그인 화면이 만료되었습니다. 다시 시도하세요.", username=username
        )

    if not verify_credentials(
        settings.admin_username, settings.admin_password, username, _field(form, "password")
    ):
        limiter.record_failure(client_ip)
        log.warning("admin login failed for user %r from %s", username[:64], client_ip)
        return _login_page(
            request, status=401, error="아이디 또는 비밀번호가 올바르지 않습니다.", username=username
        )

    limiter.reset(client_ip)
    sessions = request.app[SESSIONS]
    session = sessions.create(settings.admin_username)
    log.info("admin login succeeded from %s", client_ip)
    response = web.HTTPSeeOther("/")
    response.set_cookie(
        SESSION_COOKIE,
        sessions.cookie_value(session),
        httponly=True,
        samesite="Strict",
        path="/",
        max_age=sessions.ttl,
    )
    response.del_cookie(LOGIN_CSRF_COOKIE, path="/")
    raise response


@routes.post("/logout")
@login_required
async def logout(request: web.Request) -> web.StreamResponse:
    session: Session = request[SESSION]
    await read_protected_form(request, session)
    request.app[SESSIONS].destroy(session)
    response = web.HTTPSeeOther("/login")
    response.del_cookie(SESSION_COOKIE, path="/")
    raise response


# ---------------------------------------------------------------------------
# 라우팅 관리
# ---------------------------------------------------------------------------


@routes.get("/")
@login_required
async def dashboard(request: web.Request) -> web.StreamResponse:
    return await _render_dashboard(request, request[SESSION])


@routes.post("/routes")
@login_required
async def create_route(request: web.Request) -> web.StreamResponse:
    session: Session = request[SESSION]
    form = await read_protected_form(request, session)
    service = request.app[ROUTE_SERVICE]
    data, errors, values = parse_route_form(form, service.policy)
    if data is None:
        return await _render_dashboard(request, session, status=400, values=values, errors=errors)
    try:
        route = await service.create(data)
    except DuplicateDomainError:
        errors = {"domain": f"'{data.domain}' 은(는) 이미 등록된 도메인입니다."}
        return await _render_dashboard(request, session, status=400, values=values, errors=errors)
    session.flash("success", f"{route.domain} → {route.target} 라우팅을 추가했습니다.")
    raise web.HTTPSeeOther("/")


@routes.get(r"/routes/{route_id:\d+}/edit")
@login_required
async def edit_route_page(request: web.Request) -> web.StreamResponse:
    session: Session = request[SESSION]
    route = await request.app[ROUTE_SERVICE].get(_route_id(request))
    if route is None:
        raise web.HTTPNotFound(text="404 Not Found: route does not exist\n")
    return render(
        request,
        "edit.html",
        session=session,
        route=route,
        values=_route_values(route),
        errors={},
        flashes=session.pop_flashes(),
    )


@routes.post(r"/routes/{route_id:\d+}")
@login_required
async def update_route(request: web.Request) -> web.StreamResponse:
    session: Session = request[SESSION]
    form = await read_protected_form(request, session)
    service = request.app[ROUTE_SERVICE]
    route = await service.get(_route_id(request))
    if route is None:
        raise web.HTTPNotFound(text="404 Not Found: route does not exist\n")

    data, errors, values = parse_route_form(form, service.policy)
    if data is not None:
        try:
            updated = await service.update(route.id, data)
        except RouteNotFoundError:
            raise web.HTTPNotFound(text="404 Not Found: route does not exist\n") from None
        except DuplicateDomainError:
            errors = {"domain": f"'{data.domain}' 은(는) 이미 등록된 도메인입니다."}
        else:
            session.flash("success", f"{updated.domain} → {updated.target} 라우팅을 수정했습니다.")
            raise web.HTTPSeeOther("/")
    return render(
        request,
        "edit.html",
        status=400,
        session=session,
        route=route,
        values=values,
        errors=errors,
        flashes=[],
    )


@routes.post(r"/routes/{route_id:\d+}/toggle")
@login_required
async def toggle_route(request: web.Request) -> web.StreamResponse:
    session: Session = request[SESSION]
    await read_protected_form(request, session)
    service = request.app[ROUTE_SERVICE]
    route = await service.get(_route_id(request))
    if route is None:
        raise web.HTTPNotFound(text="404 Not Found: route does not exist\n")
    try:
        updated = await service.set_enabled(route.id, not route.enabled)
    except RouteNotFoundError:
        raise web.HTTPNotFound(text="404 Not Found: route does not exist\n") from None
    state = "활성화" if updated.enabled else "비활성화"
    session.flash("success", f"{updated.domain} 라우팅을 {state}했습니다.")
    raise web.HTTPSeeOther("/")


@routes.post(r"/routes/{route_id:\d+}/delete")
@login_required
async def delete_route(request: web.Request) -> web.StreamResponse:
    session: Session = request[SESSION]
    await read_protected_form(request, session)
    service = request.app[ROUTE_SERVICE]
    route = await service.get(_route_id(request))
    if route is None:
        raise web.HTTPNotFound(text="404 Not Found: route does not exist\n")
    try:
        await service.delete(route.id)
    except RouteNotFoundError:
        raise web.HTTPNotFound(text="404 Not Found: route does not exist\n") from None
    session.flash("success", f"{route.domain} 라우팅을 삭제했습니다.")
    raise web.HTTPSeeOther("/")


# ---------------------------------------------------------------------------
# Health check (인증/IP 제한 없음, 민감 정보 없음)
# ---------------------------------------------------------------------------


@routes.get("/healthz")
async def healthz(request: web.Request) -> web.StreamResponse:
    try:
        await request.app[REPOSITORY].ping()
    except Exception:
        log.exception("health check failed")
        return web.json_response({"status": "error"}, status=503)
    return web.json_response({"status": "ok"})
