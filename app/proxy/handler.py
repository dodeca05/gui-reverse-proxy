"""Reverse proxy 요청 처리.

요청/응답 본문은 메모리에 모으지 않고 받은 즉시 흘려보낸다. SSE(text/event-stream) 와
MCP Streamable HTTP 응답도 일반 응답과 같은 경로로 처리되며, 본문을 해석하거나 변환하지 않는다.

타임아웃 정책
  - 연결: PROXY_CONNECT_TIMEOUT (풀 대기 + TCP 연결)
  - 응답 헤더 대기: PROXY_READ_TIMEOUT. 요청 본문을 보내는 동안에는 청크마다 연장되는
    '유휴' 타임아웃이므로 느린 대용량 업로드도 끊기지 않는다.
  - 응답 본문 스트리밍: 타임아웃 없음 (SSE 장기 연결). 클라이언트가 끊으면 handler 가
    취소되고 업스트림 연결도 즉시 닫힌다.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator, Callable

import aiohttp
from aiohttp import web
from multidict import CIMultiDict
from yarl import URL

from ..config import Settings
from ..database.models import Route
from ..net import HostInfo, ip_in_networks, resolve_client_ip
from .headers import (
    ForwardingInfo,
    build_downstream_headers,
    build_upstream_headers,
    has_via_token,
)
from .resolver import RestrictedResolver
from .router import RouteTable

log = logging.getLogger(__name__)

ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
# 재사용한 keep-alive 연결이 막 닫힌 경우 한 번 재시도해도 안전한 요청
_RETRY_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def create_upstream_session(settings: Settings) -> aiohttp.ClientSession:
    """백엔드 연결용 ClientSession (커넥션 풀 공유)."""
    keepalive = settings.upstream_keepalive_timeout
    connector = aiohttp.TCPConnector(
        limit=settings.upstream_pool_limit,
        limit_per_host=0,
        use_dns_cache=False,  # 컨테이너 재생성으로 IP 가 바뀌어도 새 연결은 항상 다시 해석
        keepalive_timeout=keepalive if keepalive > 0 else None,
        force_close=keepalive <= 0,
        resolver=RestrictedResolver(settings.allowed_target_networks),
    )
    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=settings.connect_timeout or None,
        sock_read=None,  # 스트리밍 응답에 일반 HTTP 타임아웃을 걸지 않는다
    )
    return aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        auto_decompress=False,  # Content-Encoding 그대로 중계
        cookie_jar=aiohttp.DummyCookieJar(),  # 백엔드 쿠키를 프록시가 저장/공유하지 않음
        skip_auto_headers=("User-Agent", "Accept", "Accept-Encoding", "Content-Type"),
        trust_env=False,
        requote_redirect_url=False,
    )


class ProxyHandler:
    def __init__(self, settings: Settings, table: RouteTable, session: aiohttp.ClientSession) -> None:
        self._settings = settings
        self._table = table
        self._session = session
        self._via_token = f"reverse-proxy-{secrets.token_hex(4)}"
        self._via = f"1.1 {self._via_token}"

    async def handle(self, request: web.Request, host: HostInfo) -> web.StreamResponse:
        if request.method not in ALLOWED_METHODS:
            return error_response(
                405, "Method Not Allowed", headers={"Allow": ", ".join(sorted(ALLOWED_METHODS))}
            )
        if not request.raw_path.startswith("/"):
            # absolute-form(GET http://other-host/ ...) 등 포워드 프록시 형태 요청은 받지 않는다
            return error_response(400, "Bad Request: only origin-form request targets are accepted")

        route = self._table.match(host.hostname)
        if route is None:
            return error_response(404, "Not Found: no route is configured for this host")

        if has_via_token(request.headers, self._via_token):
            log.error("routing loop detected: %s -> %s", route.domain, route.target)
            return error_response(508, "Loop Detected")

        url = URL(f"http://{route.target}{request.raw_path}", encoded=True)
        if url.host != route.target_service or url.port != route.target_port:
            return error_response(400, "Bad Request: invalid request target")

        headers = build_upstream_headers(
            request.headers,
            self._forwarding_info(request, host),
            upstream_host=host.raw.strip() if route.preserve_host else route.target,
            via=self._via,
        )

        try:
            upstream = await self._send(request, url, headers)
        except TimeoutError:
            log.warning("upstream timeout: %s %s -> %s", request.method, route.domain, route.target)
            return error_response(504, "Gateway Timeout")
        except (aiohttp.ClientError, OSError) as exc:
            log.warning(
                "upstream error: %s %s -> %s: %s", request.method, route.domain, route.target, exc
            )
            return error_response(502, "Bad Gateway")

        return await self._relay(request, route, upstream)

    def _forwarding_info(self, request: web.Request, host: HostInfo) -> ForwardingInfo:
        peer = request.remote or ""
        trusted = ip_in_networks(peer, self._settings.trusted_proxies)
        client_ip = resolve_client_ip(
            peer, request.headers.getall("X-Forwarded-For", ()), self._settings.trusted_proxies
        )
        return ForwardingInfo(
            peer_ip=peer,
            client_ip=client_ip or peer,
            peer_trusted=trusted,
            host=host.raw.strip(),
        )

    async def _send(
        self, request: web.Request, url: URL, headers: CIMultiDict[str]
    ) -> aiohttp.ClientResponse:
        has_body = request.body_exists
        attempts = 2 if not has_body and request.method in _RETRY_SAFE_METHODS else 1
        read_timeout = self._settings.read_timeout or None

        for attempt in range(1, attempts + 1):
            try:
                async with asyncio.timeout(read_timeout) as deadline:
                    idle = _IdleDeadline(deadline, read_timeout)
                    body = _stream_body(request, idle.touch) if has_body else None
                    try:
                        return await self._session.request(
                            request.method,
                            url,
                            headers=headers,
                            data=body,
                            allow_redirects=False,
                        )
                    finally:
                        idle.active = False
            except (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError) as exc:
                stale = not isinstance(exc, aiohttp.ClientConnectorError)
                if attempt < attempts and stale:
                    log.debug("retrying %s %s after stale connection: %s", request.method, url, exc)
                    continue
                raise
        raise AssertionError("unreachable")

    async def _relay(
        self, request: web.Request, route: Route, upstream: aiohttp.ClientResponse
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=upstream.status,
            reason=upstream.reason,
            headers=build_downstream_headers(upstream.headers),
        )
        completed = False
        try:
            await response.prepare(request)
            while True:
                try:
                    chunk = await upstream.content.readany()
                except (aiohttp.ClientError, TimeoutError) as exc:
                    # 이미 헤더를 보냈으므로 오류 응답 대신 연결을 끊어 응답이 잘렸음을 알린다
                    log.warning(
                        "upstream aborted response body: %s -> %s: %r", route.domain, route.target, exc
                    )
                    _abort(request)
                    return response
                if not chunk:
                    break
                await response.write(chunk)
            await response.write_eof()
            completed = True
        except ConnectionResetError:
            log.debug("client disconnected while streaming %s", route.domain)
        finally:
            # 끝까지 읽지 못한 응답은 연결을 닫아 백엔드 스트림(SSE 등)도 정리되게 한다
            if completed:
                upstream.release()
            else:
                upstream.close()
        return response


class _IdleDeadline:
    """요청 본문을 보내는 동안 asyncio.timeout 을 청크마다 연장한다."""

    def __init__(self, deadline: asyncio.Timeout, timeout: float | None) -> None:
        self._deadline = deadline
        self._timeout = timeout
        self._loop = asyncio.get_running_loop()
        self.active = True

    def touch(self) -> None:
        if not self.active or self._timeout is None:
            return
        try:
            self._deadline.reschedule(self._loop.time() + self._timeout)
        except RuntimeError:  # 이미 만료/종료된 타임아웃
            pass


async def _stream_body(request: web.Request, on_chunk: Callable[[], None]) -> AsyncIterator[bytes]:
    async for chunk in request.content.iter_any():
        on_chunk()
        yield chunk


def _abort(request: web.Request) -> None:
    transport = request.transport
    if transport is not None:
        transport.abort()


def error_response(
    status: int, message: str, *, headers: dict[str, str] | None = None
) -> web.Response:
    response = web.Response(status=status, text=f"{status} {message}\n", headers=headers)
    response.headers["Cache-Control"] = "no-store"
    return response
