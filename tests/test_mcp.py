"""MCP 통신 중계 (7, 8번 항목): Streamable HTTP, SSE 스트리밍, 구버전 HTTP+SSE, 장시간 연결."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiohttp
import pytest

from .conftest import ProxyServer, Server

MCP_HOST = "mcp.test.com"
PROTOCOL = "2025-06-18"
ACCEPT_BOTH = "application/json, text/event-stream"


class SseReader:
    """aiohttp 응답에서 SSE 이벤트(id, event, data)를 하나씩 읽는다."""

    def __init__(self, resp: aiohttp.ClientResponse) -> None:
        self._resp = resp

    async def next(self) -> dict[str, Any]:
        event: dict[str, Any] = {}
        while True:
            raw = await self._resp.content.readline()
            if not raw:
                raise EOFError("stream closed")
            line = raw.decode().rstrip("\r\n")
            if not line:
                if event:
                    return event
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "data":
                event["data"] = value
                try:
                    event["json"] = json.loads(value)
                except ValueError:
                    pass
            else:
                event[field] = value


@pytest.fixture
async def mcp(backend_factory) -> Server:
    return await backend_factory("mcp", heartbeat_interval=0.8)


@pytest.fixture
async def mcp_proxy(proxy: ProxyServer, mcp: Server) -> ProxyServer:
    await proxy.add_route(MCP_HOST, mcp.port)
    return proxy


def mcp_headers(session_id: str | None = None, **extra: str) -> dict[str, str]:
    headers = {"Host": MCP_HOST, "Accept": ACCEPT_BOTH, "Content-Type": "application/json"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
        headers["MCP-Protocol-Version"] = PROTOCOL
    headers.update(extra)
    return headers


async def initialize(http: aiohttp.ClientSession, proxy: ProxyServer) -> str:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    }
    async with http.post(proxy.url("/mcp"), headers=mcp_headers(), json=body) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        result = await resp.json()
        session_id = resp.headers["Mcp-Session-Id"]
    assert result["result"]["protocolVersion"] == PROTOCOL

    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    async with http.post(proxy.url("/mcp"), headers=mcp_headers(session_id), json=note) as resp:
        assert resp.status == 202
    return session_id


async def test_mcp_json_rpc_over_post(mcp_proxy: ProxyServer, http: aiohttp.ClientSession):
    session_id = await initialize(http, mcp_proxy)

    request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    async with http.post(mcp_proxy.url("/mcp"), headers=mcp_headers(session_id), json=request) as resp:
        assert resp.status == 200
        tools = (await resp.json())["result"]["tools"]
    assert {t["name"] for t in tools} >= {"echo", "add", "countdown", "inspect_headers"}

    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "add", "arguments": {"a": 2, "b": 40}}}
    async with http.post(mcp_proxy.url("/mcp"), headers=mcp_headers(session_id), json=call) as resp:
        result = await resp.json()
    assert result == {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "42"}], "isError": False}}


async def test_mcp_streaming_response_is_relayed_in_real_time(mcp_proxy: ProxyServer, http: aiohttp.ClientSession):
    session_id = await initialize(http, mcp_proxy)
    count, interval = 4, 0.3
    call = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "countdown",
            "arguments": {"count": count, "interval": interval},
            "_meta": {"progressToken": "tok-1"},
        },
    }
    started = time.monotonic()
    async with http.post(mcp_proxy.url("/mcp"), headers=mcp_headers(session_id), json=call) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"
        reader = SseReader(resp)
        progress_times = []
        while True:
            event = await reader.next()
            message = event["json"]
            assert event["id"]
            if message.get("method") == "notifications/progress":
                progress_times.append(time.monotonic() - started)
            if message.get("id") == 7:
                final = message
                break

    assert final["result"]["content"][0]["text"] == f"countdown finished after {count} steps"
    assert len(progress_times) == count
    # 진행 알림은 모아서가 아니라 생성 즉시 하나씩 도착한다
    for i, t in enumerate(progress_times, start=1):
        assert abs(t - interval * i) < 0.25, progress_times


async def test_mcp_headers_are_forwarded(mcp_proxy: ProxyServer, http: aiohttp.ClientSession):
    session_id = await initialize(http, mcp_proxy)
    extra = {
        "Mcp-Method": "tools/call",
        "Mcp-Name": "inspect_headers",
        "Mcp-Param-Region": "ap-northeast-2",
        "Last-Event-ID": "evt-41",
        "Authorization": "Bearer mcp-token",
        "Origin": "http://mcp.test.com",
    }
    call = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "inspect_headers", "arguments": {}}}
    async with http.post(mcp_proxy.url("/mcp"), headers=mcp_headers(session_id, **extra), json=call) as resp:
        seen = json.loads((await resp.json())["result"]["content"][0]["text"])

    assert seen["mcp-session-id"] == session_id
    assert seen["mcp-protocol-version"] == PROTOCOL
    assert seen["mcp-method"] == "tools/call"
    assert seen["mcp-name"] == "inspect_headers"
    assert seen["mcp-param-region"] == "ap-northeast-2"
    assert seen["last-event-id"] == "evt-41"
    assert seen["authorization"] == "Bearer mcp-token"
    assert seen["origin"] == "http://mcp.test.com"
    assert seen["accept"] == ACCEPT_BOTH
    assert seen["host"] == MCP_HOST


async def test_mcp_error_status_and_content_type_unchanged(mcp_proxy: ProxyServer, http: aiohttp.ClientSession):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    async with http.post(mcp_proxy.url("/mcp"), headers=mcp_headers(), json=ping) as resp:
        assert resp.status == 400  # Mcp-Session-Id 누락
        assert resp.headers["Content-Type"].startswith("application/json")
    async with http.post(mcp_proxy.url("/mcp"), headers=mcp_headers("unknown-session"), json=ping) as resp:
        assert resp.status == 404  # 세션 없음 → 클라이언트가 재초기화해야 함
    async with http.get(mcp_proxy.url("/mcp"), headers={"Host": MCP_HOST, "Accept": "application/json"}) as resp:
        assert resp.status == 406


async def test_mcp_long_lived_get_stream(proxy_factory, mcp: Server, http: aiohttp.ClientSession):
    """일반 HTTP 타임아웃(0.5초)보다 긴 이벤트 간격과 연결 시간을 견디는지 확인한다."""
    proxy = await proxy_factory(read_timeout=0.5)
    await proxy.add_route(MCP_HOST, mcp.port)
    session_id = await initialize(http, proxy)

    headers = {"Host": MCP_HOST, "Accept": "text/event-stream", "Mcp-Session-Id": session_id}
    started = time.monotonic()
    async with http.get(proxy.url("/mcp"), headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"
        reader = SseReader(resp)
        beats = [await reader.next() for _ in range(4)]  # 0.8초 간격 → 약 3.2초 유지
        assert time.monotonic() - started > 3.0
        assert [b["json"]["params"]["data"]["heartbeat"] for b in beats] == [1, 2, 3, 4]

        # 스트림이 열려 있는 동안 같은 세션으로 다른 요청도 처리된다
        ping = {"jsonrpc": "2.0", "id": 5, "method": "ping"}
        async with http.post(proxy.url("/mcp"), headers=mcp_headers(session_id), json=ping) as ping_resp:
            assert (await ping_resp.json())["result"] == {}
        assert (await _stats(http, mcp))["active_streams"] == 1

    # 클라이언트 종료 후 백엔드 스트림 정리
    for _ in range(40):
        if (await _stats(http, mcp))["active_streams"] == 0:
            break
        await asyncio.sleep(0.05)
    assert (await _stats(http, mcp))["active_streams"] == 0


async def test_mcp_resume_with_last_event_id(mcp_proxy: ProxyServer, http: aiohttp.ClientSession):
    session_id = await initialize(http, mcp_proxy)
    headers = {
        "Host": MCP_HOST,
        "Accept": "text/event-stream",
        "Mcp-Session-Id": session_id,
        "Last-Event-ID": "get-10",
    }
    async with http.get(mcp_proxy.url("/mcp"), headers=headers) as resp:
        event = await SseReader(resp).next()
    assert event["id"] == "get-11"
    assert event["json"]["params"]["data"]["resumed_after"] == "get-10"


async def test_mcp_session_delete(mcp_proxy: ProxyServer, http: aiohttp.ClientSession):
    session_id = await initialize(http, mcp_proxy)
    async with http.delete(mcp_proxy.url("/mcp"), headers=mcp_headers(session_id)) as resp:
        assert resp.status == 200
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    async with http.post(mcp_proxy.url("/mcp"), headers=mcp_headers(session_id), json=ping) as resp:
        assert resp.status == 404


async def test_legacy_http_sse_transport(mcp_proxy: ProxyServer, http: aiohttp.ClientSession):
    """구버전(2024-11-05) HTTP+SSE: GET /sse 로 endpoint 를 받고 POST 응답은 SSE 로 수신."""
    async with http.get(mcp_proxy.url("/sse"), headers={"Host": MCP_HOST, "Accept": "text/event-stream"}) as stream:
        assert stream.headers["Content-Type"] == "text/event-stream"
        reader = SseReader(stream)
        endpoint = await reader.next()
        assert endpoint["event"] == "endpoint"
        post_path = endpoint["data"]
        assert post_path.startswith("/messages?session_id=")

        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}},
        }
        async with http.post(mcp_proxy.url(post_path), headers={"Host": MCP_HOST}, json=init) as resp:
            assert resp.status == 202
        message = (await reader.next())["json"]
        assert message["id"] == 1
        assert message["result"]["protocolVersion"] == "2024-11-05"

        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "안녕"}}}
        async with http.post(mcp_proxy.url(post_path), headers={"Host": MCP_HOST}, json=call) as resp:
            assert resp.status == 202
        message = (await reader.next())["json"]
        assert message["result"]["content"][0]["text"] == "안녕"


async def _stats(http: aiohttp.ClientSession, server: Server) -> dict:
    async with http.get(server.url("/debug/stats")) as resp:
        return await resp.json()
