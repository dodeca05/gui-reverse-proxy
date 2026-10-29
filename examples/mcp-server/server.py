"""프록시 검증용 MCP 테스트 서버 (aiohttp, MCP SDK 의존성 없음).

지원 전송 방식
  - Streamable HTTP (프로토콜 2025-03-26 이상)  : POST/GET/DELETE /mcp
  - HTTP+SSE (프로토콜 2024-11-05, 구버전)      : GET /sse + POST /messages?session_id=...

도구(tools)
  echo             {"text": str}                       → 그대로 반환
  add              {"a": number, "b": number}          → 합계
  countdown        {"count": int, "interval": number}  → interval 초마다 진행 알림을 SSE 로 보낸 뒤 결과 반환
  inspect_headers  {}                                  → 서버가 받은 MCP 관련 HTTP 헤더

GET /mcp 스트림은 MCP_HEARTBEAT_SECONDS 마다 notifications/message 를 보낸다 (장시간 연결 검증용).
GET /debug/stats 는 현재 열린 SSE 스트림 수를 반환한다 (연결 정리 검증용).

참고: 실제 서비스용 MCP 서버는 DNS rebinding 방지를 위해 Origin 헤더를 검증해야 한다.
이 서버는 프록시 테스트 목적이므로 모든 Origin 을 허용한다.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_VERSION = SUPPORTED_VERSIONS[0]
SERVER_INFO = {"name": "proxy-test-mcp-server", "version": "1.0.0"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "Echo back the given text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "countdown",
        "description": "Send progress notifications every `interval` seconds, then return.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 1, "maximum": 100},
                "interval": {"type": "number", "minimum": 0, "maximum": 60},
            },
        },
    },
    {
        "name": "inspect_headers",
        "description": "Return the MCP-related HTTP headers received by the server.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

INSPECTED_HEADERS = {
    "accept",
    "authorization",
    "content-type",
    "host",
    "last-event-id",
    "mcp-method",
    "mcp-name",
    "mcp-protocol-version",
    "mcp-session-id",
    "origin",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
}

Notify = Callable[[dict[str, Any]], Awaitable[None]]
SSE_HEADERS = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def sse_event(message: dict[str, Any] | None, event_id: str | None = None, event: str = "message") -> bytes:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(message, separators=(',', ':'))}")
    return ("\n".join(lines) + "\n\n").encode()


def text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def log_notification(data: dict[str, Any], logger: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "notifications/message",
        "params": {"level": "info", "logger": logger, "data": data},
    }


def jsonrpc_error_response(status: int, code: int, message: str, msg_id: Any = None) -> web.Response:
    return web.json_response(
        {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}, status=status
    )


class McpTestServer:
    def __init__(self, heartbeat_interval: float = 30.0) -> None:
        self.heartbeat_interval = heartbeat_interval
        self.sessions: dict[str, dict[str, Any]] = {}
        self.legacy_sessions: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.active_streams = 0
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ JSON-RPC
    async def handle_message(
        self, message: dict[str, Any], headers: Any, notify: Notify
    ) -> dict[str, Any] | None:
        """요청이면 JSON-RPC 응답을, 알림/응답이면 None 을 반환한다."""
        if "method" not in message or "id" not in message:
            return None
        msg_id = message["id"]
        try:
            result = await self._dispatch(message["method"], message.get("params") or {}, headers, notify)
        except JsonRpcError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": exc.code, "message": exc.message}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    async def _dispatch(self, method: str, params: dict[str, Any], headers: Any, notify: Notify) -> Any:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}, "logging": {}},
                "serverInfo": SERVER_INFO,
                "instructions": "Test server for verifying reverse proxy MCP streaming.",
            }
        if method == "ping":
            return {}
        if method == "logging/setLevel":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return await self._call_tool(params, headers, notify)
        raise JsonRpcError(-32601, f"Method not found: {method}")

    async def _call_tool(self, params: dict[str, Any], headers: Any, notify: Notify) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            return text_result(str(args.get("text", "")))
        if name == "add":
            try:
                total = float(args["a"]) + float(args["b"])
            except (KeyError, TypeError, ValueError):
                raise JsonRpcError(-32602, "add requires numeric 'a' and 'b'") from None
            return text_result(str(int(total) if total.is_integer() else total))
        if name == "countdown":
            count = max(1, min(100, int(args.get("count", 3))))
            interval = max(0.0, min(60.0, float(args.get("interval", 0.5))))
            progress_token = (params.get("_meta") or {}).get("progressToken")
            for step in range(1, count + 1):
                await asyncio.sleep(interval)
                if progress_token is not None:
                    await notify(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/progress",
                            "params": {
                                "progressToken": progress_token,
                                "progress": step,
                                "total": count,
                                "message": f"step {step}/{count}",
                            },
                        }
                    )
                await notify(
                    log_notification({"step": step, "total": count, "sent_at": time.time()}, "countdown")
                )
            return text_result(f"countdown finished after {count} steps")
        if name == "inspect_headers":
            seen = {
                k.lower(): v
                for k, v in headers.items()
                if k.lower() in INSPECTED_HEADERS or k.lower().startswith("mcp-param-")
            }
            return text_result(json.dumps(seen, sort_keys=True))
        raise JsonRpcError(-32602, f"Unknown tool: {name}")

    # ------------------------------------------------------------------ Streamable HTTP
    def _check_session(self, request: web.Request) -> web.Response | str:
        session_id = request.headers.get("Mcp-Session-Id")
        if not session_id:
            return jsonrpc_error_response(400, -32600, "Bad Request: Mcp-Session-Id header is required")
        if session_id not in self.sessions:
            return jsonrpc_error_response(404, -32001, "Session not found")
        version = request.headers.get("MCP-Protocol-Version")
        if version is not None and version not in SUPPORTED_VERSIONS:
            return jsonrpc_error_response(400, -32600, f"Unsupported MCP-Protocol-Version: {version}")
        return session_id

    async def mcp_post(self, request: web.Request) -> web.StreamResponse:
        try:
            payload = json.loads(await request.read())
        except ValueError:
            return jsonrpc_error_response(400, -32700, "Parse error")
        messages = payload if isinstance(payload, list) else [payload]
        if not messages or not all(isinstance(m, dict) for m in messages):
            return jsonrpc_error_response(400, -32600, "Invalid Request")

        if any(m.get("method") == "initialize" for m in messages):
            if len(messages) != 1:
                return jsonrpc_error_response(400, -32600, "initialize must not be batched")
            response = await self.handle_message(messages[0], request.headers, _noop)
            session_id = uuid.uuid4().hex
            self.sessions[session_id] = {
                "protocol_version": response["result"]["protocolVersion"],
                "created_at": time.time(),
            }
            return web.json_response(response, headers={"Mcp-Session-Id": session_id})

        checked = self._check_session(request)
        if isinstance(checked, web.Response):
            return checked

        requests = [m for m in messages if "method" in m and "id" in m]
        if not requests:
            return web.Response(status=202)  # 알림/응답만 있는 경우

        wants_sse = "text/event-stream" in request.headers.get("Accept", "")
        streams = any(
            m["method"] == "tools/call" and (m.get("params") or {}).get("name") == "countdown"
            for m in requests
        )
        if not (wants_sse and streams):
            responses = [await self.handle_message(m, request.headers, _noop) for m in requests]
            return web.json_response(responses if isinstance(payload, list) else responses[0])

        response = web.StreamResponse(headers=SSE_HEADERS)
        await response.prepare(request)
        stream_id = uuid.uuid4().hex[:8]
        counter = itertools.count(1)

        async def send(message: dict[str, Any]) -> None:
            await response.write(sse_event(message, f"{stream_id}-{next(counter)}"))

        self.active_streams += 1
        try:
            for message in requests:
                result = await self.handle_message(message, request.headers, send)
                if result is not None:
                    await send(result)
            await response.write_eof()
        except ConnectionResetError:
            pass
        finally:
            self.active_streams -= 1
        return response

    async def mcp_get(self, request: web.Request) -> web.StreamResponse:
        if "text/event-stream" not in request.headers.get("Accept", ""):
            return web.Response(status=406, text="Not Acceptable: text/event-stream required\n")
        checked = self._check_session(request)
        if isinstance(checked, web.Response):
            return checked

        last_event_id = request.headers.get("Last-Event-ID")
        seq = 1
        if last_event_id and last_event_id.startswith("get-"):
            try:
                seq = int(last_event_id.removeprefix("get-")) + 1
            except ValueError:
                seq = 1

        response = web.StreamResponse(headers=SSE_HEADERS)
        await response.prepare(request)
        self.active_streams += 1
        try:
            if last_event_id:
                await response.write(
                    sse_event(log_notification({"resumed_after": last_event_id}, "stream"), f"get-{seq}")
                )
                seq += 1
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                note = log_notification({"heartbeat": seq, "sent_at": time.time()}, "stream")
                await response.write(sse_event(note, f"get-{seq}"))
                seq += 1
        except ConnectionResetError:
            pass
        finally:
            self.active_streams -= 1
        return response

    async def mcp_delete(self, request: web.Request) -> web.Response:
        checked = self._check_session(request)
        if isinstance(checked, web.Response):
            return checked
        self.sessions.pop(checked, None)
        return web.Response(status=200)

    # ------------------------------------------------------------------ HTTP+SSE (2024-11-05)
    async def legacy_sse(self, request: web.Request) -> web.StreamResponse:
        session_id = uuid.uuid4().hex
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.legacy_sessions[session_id] = queue
        response = web.StreamResponse(headers=SSE_HEADERS)
        await response.prepare(request)
        self.active_streams += 1
        try:
            await response.write(f"event: endpoint\ndata: /messages?session_id={session_id}\n\n".encode())
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=self.heartbeat_interval)
                except TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                await response.write(sse_event(message))
        except ConnectionResetError:
            pass
        finally:
            self.legacy_sessions.pop(session_id, None)
            self.active_streams -= 1
        return response

    async def legacy_messages(self, request: web.Request) -> web.Response:
        queue = self.legacy_sessions.get(request.query.get("session_id", ""))
        if queue is None:
            return web.Response(status=404, text="Session not found\n")
        try:
            payload = json.loads(await request.read())
        except ValueError:
            return web.Response(status=400, text="Parse error\n")
        messages = payload if isinstance(payload, list) else [payload]
        headers = request.headers.copy()
        for message in messages:
            if isinstance(message, dict):
                task = asyncio.create_task(self._legacy_reply(queue, message, headers))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        return web.Response(status=202, text="Accepted")

    async def _legacy_reply(self, queue: asyncio.Queue[dict[str, Any]], message: dict[str, Any], headers: Any) -> None:
        response = await self.handle_message(message, headers, queue.put)
        if response is not None:
            await queue.put(response)

    # ------------------------------------------------------------------ misc
    async def stats(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "active_streams": self.active_streams,
                "sessions": len(self.sessions),
                "legacy_sessions": len(self.legacy_sessions),
            }
        )

    async def healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})


async def _noop(message: dict[str, Any]) -> None:
    return None


MCP_SERVER = web.AppKey("mcp_server", McpTestServer)


def create_app(heartbeat_interval: float = 30.0) -> web.Application:
    server = McpTestServer(heartbeat_interval)
    app = web.Application()
    app[MCP_SERVER] = server
    app.router.add_post("/mcp", server.mcp_post)
    app.router.add_get("/mcp", server.mcp_get, allow_head=False)
    app.router.add_delete("/mcp", server.mcp_delete)
    app.router.add_get("/sse", server.legacy_sse, allow_head=False)
    app.router.add_post("/messages", server.legacy_messages)
    app.router.add_get("/debug/stats", server.stats)
    app.router.add_get("/healthz", server.healthz)
    return app


if __name__ == "__main__":
    web.run_app(
        create_app(float(os.environ.get("MCP_HEARTBEAT_SECONDS", "30"))),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "3000")),
        handler_cancellation=True,
    )
