"""HTTP reverse proxy 동작: 라우팅, 메서드, 본문 스트리밍, 헤더, 오류 처리."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import time

import aiohttp
import pytest

from .conftest import ProxyServer, Server, test_app_module

TEST_HOST = {"Host": "test.com"}


def pattern_sha256(size: int) -> str:
    digest = hashlib.sha256()
    for chunk in test_app_module.pattern_bytes(size):
        digest.update(chunk)
    return digest.hexdigest()


def header_values(echo: dict, name: str) -> list[str]:
    return [v for k, v in echo["headers"] if k.lower() == name.lower()]


# 1. 도메인별 라우팅 --------------------------------------------------------------


async def test_routes_requests_by_domain(proxy: ProxyServer, backend_factory, http: aiohttp.ClientSession):
    test_app = await backend_factory("test-app", name="test-app")
    api = await backend_factory("test-app", name="api-server")
    await proxy.add_route("test.com", test_app.port)
    await proxy.add_route("api.test.com", api.port)

    async with http.get(proxy.url("/"), headers={"Host": "test.com"}) as resp:
        assert resp.status == 200
        assert (await resp.json())["service"] == "test-app"
    async with http.get(proxy.url("/"), headers={"Host": "api.test.com"}) as resp:
        assert (await resp.json())["service"] == "api-server"


async def test_host_header_normalization(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    for host in ("TEST.com", "test.com:80", "Test.Com.:8080"):
        async with http.get(proxy.url("/"), headers={"Host": host}) as resp:
            assert resp.status == 200, host


async def test_exact_domain_match_only(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    for host in ("sub.test.com", "www.test.com", "test.com.evil.org", "atest.com"):
        async with http.get(proxy.url("/"), headers={"Host": host}) as resp:
            assert resp.status == 404, host


# 2. 경로 / Query String 유지 -----------------------------------------------------


async def test_path_and_query_string_are_preserved(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    target = "/echo/users/a%2Fb/%ED%95%9C?q=hello%20world&tag=a&tag=b&empty=&plus=1+2"
    async with http.get(proxy.url(target), headers=TEST_HOST) as resp:
        echo = await resp.json()
    assert echo["raw_path"] == target  # 인코딩까지 원본 그대로
    assert echo["path"] == "/echo/users/a/b/한"


# 3. HTTP 메서드 -----------------------------------------------------------------


async def test_http_methods(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        async with http.request(method, proxy.url("/echo/m"), headers=TEST_HOST, data=b"x" if method in ("POST", "PUT", "PATCH") else None) as resp:
            assert resp.status == 200, method
            assert (await resp.json())["method"] == method

    async with http.head(proxy.url("/download?size=12345"), headers=TEST_HOST) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Length"] == "12345"
        assert await resp.read() == b""


async def test_unsupported_methods_rejected(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    for method in ("TRACE", "PROPFIND"):
        async with http.request(method, proxy.url("/echo"), headers=TEST_HOST) as resp:
            assert resp.status == 405
            assert "GET" in resp.headers["Allow"]


# 4. JSON 요청 / 응답 ------------------------------------------------------------


async def test_json_request_and_response(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    payload = {"name": "홍길동", "items": [1, 2, 3], "nested": {"ok": True}}
    async with http.post(proxy.url("/echo/json"), headers=TEST_HOST, json=payload) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        echo = await resp.json()
    assert echo["json"] == payload
    assert header_values(echo, "Content-Type") == ["application/json"]
    assert header_values(echo, "Content-Length") == [str(len(json.dumps(payload).encode()))]


async def test_status_codes_passthrough(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    for code in (201, 204, 400, 401, 404, 418, 500, 503):
        async with http.get(proxy.url(f"/status/{code}"), headers=TEST_HOST) as resp:
            assert resp.status == code


# 5. 대용량 본문 스트리밍 -----------------------------------------------------------


async def test_large_upload_with_content_length(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    body = os.urandom(32 * 1024 * 1024)
    async with http.post(proxy.url("/upload"), headers=TEST_HOST, data=body) as resp:
        result = await resp.json()
    assert result["bytes"] == len(body)
    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    assert result["content_length"] == str(len(body))


async def test_large_upload_chunked(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    digest = hashlib.sha256()
    total = 0

    async def generate():
        nonlocal total
        for _ in range(256):
            chunk = os.urandom(128 * 1024)
            digest.update(chunk)
            total += len(chunk)
            yield chunk

    async with http.post(proxy.url("/upload"), headers=TEST_HOST, data=generate()) as resp:
        result = await resp.json()
    assert result["bytes"] == total == 32 * 1024 * 1024
    assert result["sha256"] == digest.hexdigest()
    assert result["transfer_encoding"] == "chunked"


async def test_large_download_streaming(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    size = 48 * 1024 * 1024 + 123
    for chunked in ("0", "1"):
        digest = hashlib.sha256()
        received = 0
        async with http.get(proxy.url(f"/download?size={size}&chunked={chunked}"), headers=TEST_HOST) as resp:
            if chunked == "1":
                assert resp.headers.get("Transfer-Encoding") == "chunked"
                assert "Content-Length" not in resp.headers
            else:
                assert resp.headers["Content-Length"] == str(size)
            async for chunk in resp.content.iter_any():
                digest.update(chunk)
                received += len(chunk)
        assert received == size
        assert digest.hexdigest() == pattern_sha256(size)


async def test_content_encoding_is_not_altered(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    async with http.get(proxy.url("/gzip"), headers=TEST_HOST) as resp:
        raw = await resp.read()
        assert resp.headers["Content-Encoding"] == "gzip"
        assert resp.headers["Content-Length"] == str(len(raw))
    assert gzip.decompress(raw).startswith(b"compressed payload")

    compressed = gzip.compress(b'{"compressed": true}')
    headers = {**TEST_HOST, "Content-Encoding": "gzip", "Content-Type": "application/json"}
    async with http.post(proxy.url("/echo/gz"), headers=headers, data=compressed) as resp:
        echo = await resp.json()
    # 프록시가 본문을 풀었다면 Content-Length 가 달라지거나 백엔드의 gzip 해제가 실패한다
    assert header_values(echo, "Content-Encoding") == ["gzip"]
    assert header_values(echo, "Content-Length") == [str(len(compressed))]
    assert echo["json"] == {"compressed": True}


# 헤더 처리 ----------------------------------------------------------------------


async def test_end_to_end_headers_preserved(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    headers = {
        **TEST_HOST,
        "Authorization": "Bearer token-123",
        "Cookie": "a=1; b=2",
        "Accept": "application/json, text/event-stream",
        "X-Custom": "value",
        "Origin": "http://test.com",
    }
    skip = ["User-Agent", "Accept-Encoding"]
    async with http.get(proxy.url("/echo"), headers=headers, skip_auto_headers=skip) as resp:
        echo = await resp.json()
    for name in ("Authorization", "Cookie", "Accept", "X-Custom", "Origin"):
        assert header_values(echo, name) == [headers[name]], name
    assert header_values(echo, "Host") == ["test.com"]
    # 클라이언트가 보내지 않은 기본 헤더를 프록시가 임의로 추가하지 않는다
    assert header_values(echo, "Accept-Encoding") == []
    assert header_values(echo, "User-Agent") == []


async def test_hop_by_hop_headers_removed(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    headers = {
        **TEST_HOST,
        "Connection": "keep-alive, X-Secret-Hop",
        "Keep-Alive": "timeout=5",
        "X-Secret-Hop": "drop me",
        "Proxy-Authorization": "Basic abc",
        "TE": "trailers",
        "Upgrade": "websocket",
    }
    async with http.get(proxy.url("/echo"), headers=headers) as resp:
        echo = await resp.json()
    names = {k.lower() for k, _ in echo["headers"]}
    for name in ("x-secret-hop", "keep-alive", "proxy-authorization", "te", "upgrade"):
        assert name not in names, name


async def test_forwarded_headers_from_untrusted_client_are_replaced(
    proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession
):
    await proxy.add_route("test.com", backend.port)
    spoofed = {
        **TEST_HOST,
        "X-Forwarded-For": "1.2.3.4",
        "X-Forwarded-Host": "evil.com",
        "X-Forwarded-Proto": "https",
        "X-Real-IP": "1.2.3.4",
        "Forwarded": "for=1.2.3.4",
    }
    async with http.get(proxy.url("/echo"), headers=spoofed) as resp:
        echo = await resp.json()
    assert header_values(echo, "X-Forwarded-For") == ["127.0.0.1"]
    assert header_values(echo, "X-Forwarded-Host") == ["test.com"]
    assert header_values(echo, "X-Forwarded-Proto") == ["http"]
    assert header_values(echo, "X-Real-IP") == ["127.0.0.1"]
    assert header_values(echo, "Forwarded") == []


async def test_forwarded_headers_from_trusted_proxy_are_extended(
    proxy_factory, backend: Server, http: aiohttp.ClientSession
):
    from app.net import parse_networks

    proxy = await proxy_factory(trusted_proxies=parse_networks("127.0.0.0/8"))
    await proxy.add_route("test.com", backend.port)
    headers = {**TEST_HOST, "X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"}
    async with http.get(proxy.url("/echo"), headers=headers) as resp:
        echo = await resp.json()
    assert header_values(echo, "X-Forwarded-For") == ["203.0.113.7, 127.0.0.1"]
    assert header_values(echo, "X-Forwarded-Proto") == ["https"]
    assert header_values(echo, "X-Real-IP") == ["203.0.113.7"]


async def test_host_header_rewrite_option(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port, preserve_host=False)
    async with http.get(proxy.url("/echo"), headers=TEST_HOST) as resp:
        echo = await resp.json()
    assert header_values(echo, "Host") == [f"localhost:{backend.port}"]
    assert header_values(echo, "X-Forwarded-Host") == ["test.com"]


async def test_multiple_set_cookie_headers(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    async with http.get(proxy.url("/cookies"), headers=TEST_HOST) as resp:
        cookies = resp.headers.getall("Set-Cookie")
    assert len(cookies) == 2
    assert any(c.startswith("session=abc123") for c in cookies)
    assert any(c.startswith("theme=dark") for c in cookies)


async def test_redirects_are_not_followed(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    async with http.get(proxy.url("/redirect"), headers=TEST_HOST, allow_redirects=False) as resp:
        assert resp.status == 302
        assert resp.headers["Location"] == "/echo?redirected=1"


# 연결 / 동시성 -------------------------------------------------------------------


async def test_upstream_connection_pool_is_reused(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    peers = []
    for _ in range(5):
        async with http.get(proxy.url("/echo"), headers=TEST_HOST) as resp:
            peers.append(tuple((await resp.json())["peer"]))
    assert len(set(peers)) == 1, peers


async def test_concurrent_requests(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)

    async def one() -> int:
        async with http.get(proxy.url("/slow?delay=0.5"), headers=TEST_HOST) as resp:
            await resp.read()
            return resp.status

    started = time.monotonic()
    statuses = await asyncio.gather(*(one() for _ in range(50)))
    assert statuses == [200] * 50
    assert time.monotonic() - started < 3.0  # 순차 처리였다면 25초


# 13~15. 오류 처리 / 장애 복구 --------------------------------------------------------


async def test_unknown_and_disabled_domains_return_404(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("off.test.com", backend.port, enabled=False)
    for host in ("unknown.example", "off.test.com"):
        async with http.get(proxy.url("/"), headers={"Host": host}) as resp:
            assert resp.status == 404


async def test_backend_connection_failure_returns_502(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    dead = Server(lambda: test_app_module.create_app())
    await dead.start()
    port = dead.port
    await dead.stop()  # 아무도 듣지 않는 포트
    await proxy.add_route("down.test.com", port)
    await proxy.add_route("test.com", backend.port)

    async with http.get(proxy.url("/"), headers={"Host": "down.test.com"}) as resp:
        assert resp.status == 502
    # 다른 서비스는 영향 없이 정상 처리
    async with http.get(proxy.url("/"), headers=TEST_HOST) as resp:
        assert resp.status == 200


async def test_unresolvable_service_returns_502(proxy: ProxyServer, http: aiohttp.ClientSession):
    await proxy.add_route("ghost.test.com", 1000, service="no-such-service-xyz")
    async with http.get(proxy.url("/"), headers={"Host": "ghost.test.com"}) as resp:
        assert resp.status == 502


async def test_backend_timeout_returns_504(proxy_factory, backend: Server, http: aiohttp.ClientSession):
    proxy = await proxy_factory(read_timeout=0.5)
    await proxy.add_route("test.com", backend.port)
    started = time.monotonic()
    async with http.get(proxy.url("/slow?delay=3"), headers=TEST_HOST) as resp:
        assert resp.status == 504
    assert time.monotonic() - started < 2.0


async def test_backend_restart_recovers(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    async with http.get(proxy.url("/"), headers=TEST_HOST) as resp:
        assert resp.status == 200

    await backend.stop()
    async with http.get(proxy.url("/"), headers=TEST_HOST) as resp:
        assert resp.status == 502

    await backend.start()  # 같은 포트로 재시작
    async with http.get(proxy.url("/"), headers=TEST_HOST) as resp:
        assert resp.status == 200


async def test_backend_abort_mid_body_is_not_hidden(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    """백엔드가 본문 전송 중 끊기면 클라이언트도 '잘린 응답'으로 인지해야 한다 (정상 종료로 위장 금지)."""
    await proxy.add_route("test.com", backend.port)
    for chunked in ("0", "1"):
        async with http.get(proxy.url(f"/abort?chunked={chunked}"), headers=TEST_HOST) as resp:
            assert resp.status == 200
            with pytest.raises(aiohttp.ClientPayloadError):
                await resp.read()
    async with http.get(proxy.url("/"), headers=TEST_HOST) as resp:
        assert resp.status == 200


async def test_routing_loop_is_detected(proxy: ProxyServer, http: aiohttp.ClientSession):
    await proxy.add_route("loop.test.com", proxy.port)  # 자기 자신을 가리키는 잘못된 설정
    async with http.get(proxy.url("/"), headers={"Host": "loop.test.com"}) as resp:
        assert resp.status == 508


# 오픈 프록시 방지 ------------------------------------------------------------------


async def _raw_request(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await reader.read(65536)
    writer.close()
    await writer.wait_closed()
    return data


async def test_absolute_form_request_target_rejected(proxy: ProxyServer, backend: Server):
    await proxy.add_route("test.com", backend.port)
    data = await _raw_request(
        proxy.port, b"GET http://169.254.169.254/latest HTTP/1.1\r\nHost: test.com\r\nConnection: close\r\n\r\n"
    )
    assert data.startswith(b"HTTP/1.1 400")


async def test_missing_or_duplicate_host_rejected(proxy: ProxyServer):
    for raw in (
        b"GET / HTTP/1.0\r\n\r\n",
        b"GET / HTTP/1.1\r\nHost: a.com\r\nHost: b.com\r\nConnection: close\r\n\r\n",
    ):
        data = await _raw_request(proxy.port, raw)
        assert data.startswith(b"HTTP/1.") and b" 400 " in data.split(b"\r\n", 1)[0], data[:80]
