"""애플리케이션 진입점.

하나의 aiohttp 서버가 Host 헤더를 기준으로 요청을 나눈다.
  - Host 가 IP 주소  → 관리자 웹 UI (인증 + 관리자 IP 허용 목록)
  - Host 가 도메인   → reverse proxy (관리자 핸들러에는 절대 도달하지 않음)
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator

from aiohttp import web
from aiohttp.typedefs import Handler
from aiohttp.web_log import AccessLogger

from .admin.auth import LoginRateLimiter, SessionManager
from .admin.handlers import admin_gate, setup_admin
from .config import ConfigError, Settings
from .context import LOGIN_LIMITER, PROXY, REPOSITORY, ROUTE_SERVICE, SESSIONS, SETTINGS
from .database.repository import RouteRepository
from .net import is_ip_literal, parse_host_header
from .proxy.handler import ProxyHandler, create_upstream_session, error_response
from .proxy.router import RouteTable
from .service import RouteService
from .validation import TargetPolicy

log = logging.getLogger("app")

# 서버(RequestHandler) 옵션. 테스트에서도 동일하게 사용한다.
#  - auto_decompress=False: 요청 본문을 압축 해제하지 않고 Content-Encoding 그대로 전달
#  - handler_cancellation=True: 클라이언트가 끊으면 handler 를 취소해 업스트림 스트림도 정리
SERVER_OPTIONS = {"auto_decompress": False, "handler_cancellation": True}
ACCESS_LOG_FORMAT = '%a "%{Host}i" "%r" %s %b %Tfs "%{User-Agent}i"'


class ProxyAccessLogger(AccessLogger):
    """Docker HEALTHCHECK(컨테이너 내부 127.0.0.1 → /healthz) 요청은 접근 로그에서 제외한다."""

    def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
        if request.path == "/healthz" and request.remote in ("127.0.0.1", "::1"):
            host = parse_host_header(request.headers.get("Host", ""))
            if host is not None and is_ip_literal(host.hostname):
                return
        super().log(request, response, time)


@web.middleware
async def host_dispatch(request: web.Request, handler: Handler) -> web.StreamResponse:
    host_values = request.headers.getall("Host", [])
    host = parse_host_header(host_values[0]) if len(host_values) == 1 else None
    if host is None:
        return error_response(400, "Bad Request: missing or invalid Host header")
    if is_ip_literal(host.hostname):
        return await admin_gate(request, handler)
    return await request.app[PROXY].handle(request, host)


async def _lifespan(app: web.Application) -> AsyncIterator[None]:
    settings = app[SETTINGS]
    repository = RouteRepository(settings.database_path)
    await repository.open()
    table = RouteTable()
    service = RouteService(
        repository,
        table,
        TargetPolicy(settings.allowed_target_services, settings.blocked_target_services),
    )
    await service.load()
    session = create_upstream_session(settings)

    app[REPOSITORY] = repository
    app[ROUTE_SERVICE] = service
    app[PROXY] = ProxyHandler(settings, table, session)
    log.info("loaded %d active routes", len(table))
    try:
        yield
    finally:
        await session.close()
        await repository.close()


def create_app(settings: Settings) -> web.Application:
    app = web.Application(middlewares=[host_dispatch])
    app[SETTINGS] = settings
    app[SESSIONS] = SessionManager(settings.session_secret, settings.session_ttl)
    app[LOGIN_LIMITER] = LoginRateLimiter(settings.login_max_attempts, settings.login_lockout_seconds)
    setup_admin(app)
    app.cleanup_ctx.append(_lifespan)
    return app


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    log.info("starting reverse proxy on %s:%d", settings.listen_host, settings.listen_port)
    web.run_app(
        create_app(settings),
        host=settings.listen_host,
        port=settings.listen_port,
        access_log_class=ProxyAccessLogger,
        access_log_format=ACCESS_LOG_FORMAT,
        shutdown_timeout=settings.shutdown_timeout,
        print=None,
        **SERVER_OPTIONS,
    )


if __name__ == "__main__":
    main()
