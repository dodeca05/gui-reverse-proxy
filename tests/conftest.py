"""테스트 공용 fixture.

Docker 없이 한 프로세스 안에서 프록시와 예제 백엔드(test-app, mcp-server)를 띄운다.
Docker 서비스명 대신 ``localhost`` 를 라우팅 대상으로 쓰기 위해 테스트 설정에서는
대상 네트워크를 127.0.0.0/8 로 허용하고 차단 목록을 비운다.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import aiohttp
import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.context import ROUTE_SERVICE  # noqa: E402
from app.database.models import Route, RouteInput  # noqa: E402
from app.main import SERVER_OPTIONS, create_app  # noqa: E402
from app.net import parse_networks  # noqa: E402

ADMIN_USER = "admin"
ADMIN_PASSWORD = "correct-horse-battery"
SESSION_SECRET = "test-session-secret-0123456789-abcdefghijklmnop"


def _load_example(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


test_app_module = _load_example("example_test_app", "examples/test-app/app.py")
mcp_module = _load_example("example_mcp_server", "examples/mcp-server/server.py")


class Server:
    """aiohttp 앱을 127.0.0.1 의 임의(또는 지정) 포트에서 실행한다. 같은 포트로 재시작할 수 있다."""

    def __init__(self, app_factory: Callable[[], web.Application], **runner_kwargs: Any) -> None:
        self._factory = app_factory
        self._runner_kwargs = runner_kwargs
        self._runner: web.AppRunner | None = None
        self.app: web.Application | None = None
        self.port: int = 0

    async def start(self) -> None:
        self.app = self._factory()
        self._runner = web.AppRunner(self.app, shutdown_timeout=0.5, **self._runner_kwargs)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port, reuse_address=True)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


class ProxyServer(Server):
    def __init__(self, settings: Settings) -> None:
        super().__init__(lambda: create_app(settings), **SERVER_OPTIONS)
        self.settings = settings

    async def add_route(
        self,
        domain: str,
        port: int,
        service: str = "localhost",
        *,
        enabled: bool = True,
        preserve_host: bool = True,
    ) -> Route:
        assert self.app is not None
        return await self.app[ROUTE_SERVICE].create(
            RouteInput(domain, service, port, enabled=enabled, preserve_host=preserve_host)
        )


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "admin_username": ADMIN_USER,
        "admin_password": ADMIN_PASSWORD,
        "session_secret": SESSION_SECRET,
        "database_path": str(tmp_path / "proxy.db"),
        "admin_allowed_networks": parse_networks("127.0.0.0/8,::1/128"),
        "allowed_target_networks": parse_networks("127.0.0.0/8"),
        "blocked_target_services": frozenset(),
        "connect_timeout": 2.0,
        "read_timeout": 5.0,
        "login_max_attempts": 3,
        "login_lockout_seconds": 60,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
async def backend_factory() -> AsyncIterator[Callable[..., Any]]:
    servers: list[Server] = []

    async def factory(kind: str = "test-app", **kwargs: Any) -> Server:
        if kind == "mcp":
            server = Server(lambda: mcp_module.create_app(**kwargs), handler_cancellation=True)
        else:
            name = kwargs.get("name", kind)
            server = Server(lambda: test_app_module.create_app(name), handler_cancellation=True)
        await server.start()
        servers.append(server)
        return server

    yield factory
    for server in servers:
        await server.stop()


@pytest.fixture
async def backend(backend_factory: Callable[..., Any]) -> Server:
    return await backend_factory("test-app")


@pytest.fixture
async def proxy_factory(tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    servers: list[ProxyServer] = []

    async def factory(**overrides: Any) -> ProxyServer:
        server = ProxyServer(make_settings(tmp_path, **overrides))
        await server.start()
        servers.append(server)
        return server

    yield factory
    for server in servers:
        await server.stop()


@pytest.fixture
async def proxy(proxy_factory: Callable[..., Any]) -> ProxyServer:
    return await proxy_factory()


@pytest.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    """프록시 클라이언트 (쿠키 저장/자동 압축 해제 없음)."""
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False
    ) as session:
        yield session


# ---------------------------------------------------------------------------
# 관리자 페이지 도우미
# ---------------------------------------------------------------------------

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


class AdminClient:
    """IP 주소(127.0.0.1)로 접속해 관리자 페이지를 사용하는 브라우저 흉내."""

    def __init__(self, session: aiohttp.ClientSession, proxy: ProxyServer) -> None:
        self.session = session
        self.proxy = proxy

    def url(self, path: str) -> str:
        return self.proxy.url(path)

    async def login(
        self, username: str = ADMIN_USER, password: str = ADMIN_PASSWORD
    ) -> aiohttp.ClientResponse:
        async with self.session.get(self.url("/login")) as resp:
            token = CSRF_RE.search(await resp.text()).group(1)  # type: ignore[union-attr]
        resp = await self.session.post(
            self.url("/login"),
            data={"csrf_token": token, "username": username, "password": password},
            allow_redirects=False,
        )
        await resp.read()
        return resp

    async def csrf_token(self) -> str:
        async with self.session.get(self.url("/"), allow_redirects=False) as resp:
            assert resp.status == 200, resp.status
            return CSRF_RE.search(await resp.text()).group(1)  # type: ignore[union-attr]

    async def post(self, path: str, data: dict[str, str] | None = None, *, csrf: bool = True) -> tuple[int, str]:
        form = dict(data or {})
        if csrf:
            form["csrf_token"] = await self.csrf_token()
        async with self.session.post(self.url(path), data=form, allow_redirects=False) as resp:
            return resp.status, await resp.text()

    async def create_route(
        self, domain: str, service: str, port: int, *, enabled: bool = True, preserve_host: bool = True
    ) -> tuple[int, str]:
        data = {"domain": domain, "target_service": service, "target_port": str(port)}
        if enabled:
            data["enabled"] = "on"
        if preserve_host:
            data["preserve_host"] = "on"
        return await self.post("/routes", data)

    async def dashboard(self) -> str:
        async with self.session.get(self.url("/"), allow_redirects=False) as resp:
            assert resp.status == 200
            return await resp.text()


@pytest.fixture
async def admin(proxy: ProxyServer) -> AsyncIterator[AdminClient]:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        yield AdminClient(session, proxy)


async def route_id_for(proxy: ProxyServer, domain: str) -> int:
    assert proxy.app is not None
    for route in await proxy.app[ROUTE_SERVICE].list_routes():
        if route.domain == domain:
            return route.id
    raise AssertionError(f"route not found: {domain}")
