"""aiohttp Application 에 저장하는 공용 객체 키."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    import jinja2

    from .admin.auth import LoginRateLimiter, SessionManager
    from .config import Settings
    from .database.repository import RouteRepository
    from .proxy.handler import ProxyHandler
    from .service import RouteService

SETTINGS = web.AppKey("settings", "Settings")
REPOSITORY = web.AppKey("repository", "RouteRepository")
ROUTE_SERVICE = web.AppKey("route_service", "RouteService")
PROXY = web.AppKey("proxy", "ProxyHandler")
SESSIONS = web.AppKey("sessions", "SessionManager")
LOGIN_LIMITER = web.AppKey("login_limiter", "LoginRateLimiter")
TEMPLATES = web.AppKey("templates", "jinja2.Environment")
