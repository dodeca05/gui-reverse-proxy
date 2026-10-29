"""SSE 실시간 전달 검증 (6번 항목)."""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp

from .conftest import ProxyServer, Server


async def read_sse_events(resp: aiohttp.ClientResponse, started: float) -> list[tuple[float, dict]]:
    """SSE 이벤트를 한 줄씩 읽으며 각 data 이벤트의 도착 시각을 기록한다."""
    events: list[tuple[float, dict]] = []
    async for raw_line in resp.content:
        line = raw_line.decode().rstrip("\r\n")
        if line.startswith("data:"):
            events.append((time.monotonic() - started, json.loads(line[5:].strip())))
    return events


async def test_sse_events_are_delivered_immediately(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    await proxy.add_route("test.com", backend.port)
    count, interval = 5, 0.4

    started = time.monotonic()
    async with http.get(
        proxy.url(f"/sse?count={count}&interval={interval}"), headers={"Host": "test.com"}
    ) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"
        events = await read_sse_events(resp, started)

    assert [e["n"] for _, e in events] == list(range(1, count + 1))
    arrivals = [t for t, _ in events]
    total = interval * (count - 1)
    # 첫 이벤트는 마지막 이벤트가 만들어지기 훨씬 전에 도착해야 한다 (버퍼링 없음)
    assert arrivals[0] < 0.3, arrivals
    assert arrivals[-1] >= total - 0.05, arrivals
    # 각 이벤트가 백엔드가 보낸 간격대로 하나씩 도착
    for i in range(1, count):
        gap = arrivals[i] - arrivals[i - 1]
        assert 0.2 < gap < 0.7, (i, arrivals)


async def test_sse_client_disconnect_closes_backend_stream(proxy: ProxyServer, backend_factory, http: aiohttp.ClientSession):
    """클라이언트가 스트림 중간에 끊으면 백엔드 스트림도 정리된다."""
    mcp = await backend_factory("mcp", heartbeat_interval=0.2)
    await proxy.add_route("mcp.test.com", mcp.port)
    headers = {"Host": "mcp.test.com", "Accept": "text/event-stream"}

    async with http.get(proxy.url("/sse"), headers=headers) as resp:
        assert resp.status == 200
        first = await resp.content.readline()
        assert first.startswith(b"event: endpoint")
        assert (await _stats(http, mcp))["active_streams"] == 1
    # 컨텍스트 종료 = 클라이언트 연결 종료

    for _ in range(40):
        if (await _stats(http, mcp))["active_streams"] == 0:
            break
        await asyncio.sleep(0.05)
    assert (await _stats(http, mcp))["active_streams"] == 0


async def _stats(http: aiohttp.ClientSession, server: Server) -> dict:
    async with http.get(server.url("/debug/stats")) as resp:
        return await resp.json()
