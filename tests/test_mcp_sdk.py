"""공식 MCP Python SDK 클라이언트로 프록시를 통한 통신을 검증한다 (선택).

``pip install "mcp>=2"`` 이 설치되어 있을 때만 실행되며, 없으면 건너뛴다.
"""

from __future__ import annotations

import time
from importlib.metadata import PackageNotFoundError, version

import pytest

try:
    _MCP_MAJOR = int(version("mcp").split(".")[0])
except PackageNotFoundError:
    _MCP_MAJOR = 0
if _MCP_MAJOR < 2:
    pytest.skip("requires the official MCP Python SDK >= 2 (pip install 'mcp>=2')", allow_module_level=True)

httpx2 = pytest.importorskip("httpx2")

from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.sse import sse_client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from .conftest import ProxyServer, Server  # noqa: E402

MCP_HOST = "mcp.test.com"


@pytest.fixture
async def mcp_proxy(proxy: ProxyServer, backend_factory) -> ProxyServer:
    server: Server = await backend_factory("mcp", heartbeat_interval=30)
    await proxy.add_route(MCP_HOST, server.port)
    return proxy


async def test_sdk_streamable_http(mcp_proxy: ProxyServer):
    progress: list[float] = []

    async def on_progress(value: float, total: float | None, message: str | None) -> None:
        progress.append(time.monotonic())

    async with httpx2.AsyncClient(headers={"Host": MCP_HOST}, timeout=httpx2.Timeout(30, read=300)) as http:
        async with streamable_http_client(mcp_proxy.url("/mcp"), http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                init = await session.initialize()
                assert init.server_info.name == "proxy-test-mcp-server"
                tools = await session.list_tools()
                assert "countdown" in {t.name for t in tools.tools}

                result = await session.call_tool("add", {"a": 20, "b": 22})
                assert result.content[0].text == "42"

                started = time.monotonic()
                result = await session.call_tool(
                    "countdown", {"count": 3, "interval": 0.3}, progress_callback=on_progress
                )
                assert result.content[0].text == "countdown finished after 3 steps"
                offsets = [t - started for t in progress]
                assert len(offsets) == 3
                assert offsets[0] < 0.6, offsets  # 마지막까지 모으지 않고 즉시 전달


async def test_sdk_legacy_sse(mcp_proxy: ProxyServer):
    async with sse_client(mcp_proxy.url("/sse"), headers={"Host": MCP_HOST}) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            result = await session.call_tool("echo", {"text": "legacy"})
            assert result.content[0].text == "legacy"
