"""프록시 검증용 일반 HTTP API 백엔드 (aiohttp).

환경변수
  SERVICE_NAME  응답에 표시할 서비스 이름 (기본: test-app)
  PORT          리슨 포트 (기본: 1000)

엔드포인트
  GET  /                 서비스 정보
  *    /echo[/...]       받은 요청(메서드, 경로, 쿼리, 헤더, 본문)을 JSON 으로 반환
                         (아래에 없는 다른 모든 경로도 동일하게 echo)
  POST /upload           요청 본문을 스트리밍으로 읽어 크기와 sha256 반환
  GET  /download         ?size=바이트&chunked=0|1 - 결정적 패턴 데이터를 스트리밍
  GET  /sse              ?count=5&interval=0.5 - Server-Sent Events
  GET  /abort            ?chunked=0|1 - 본문 일부만 보내고 연결 종료
  GET  /slow             ?delay=초 - 지연 후 응답
  GET  /status/{code}    지정한 상태 코드로 응답
  GET  /cookies          Set-Cookie 헤더 여러 개
  GET  /redirect         302 리다이렉트 (프록시가 따라가지 않는지 확인용)
  GET  /gzip             Content-Encoding: gzip 본문
  GET  /healthz          상태 확인
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import os
import time

from aiohttp import web

PATTERN = bytes(range(256)) * 256  # 64 KiB


def pattern_bytes(size: int):
    """다운로드/업로드 검증용 결정적 데이터 생성기."""
    remaining = size
    while remaining > 0:
        chunk = PATTERN[: min(len(PATTERN), remaining)]
        remaining -= len(chunk)
        yield chunk


def _int(request: web.Request, name: str, default: int) -> int:
    try:
        return int(request.query.get(name, default))
    except ValueError:
        raise web.HTTPBadRequest(text=f"invalid {name}\n") from None


def _float(request: web.Request, name: str, default: float) -> float:
    try:
        return float(request.query.get(name, default))
    except ValueError:
        raise web.HTTPBadRequest(text=f"invalid {name}\n") from None


def create_app(service_name: str = "test-app") -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(request: web.Request) -> web.Response:
        return web.json_response({"service": service_name, "message": "hello from backend"})

    @routes.get("/healthz")
    async def healthz(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    @routes.route("*", "/echo")
    @routes.route("*", "/echo/{tail:.*}")
    async def echo(request: web.Request) -> web.Response:
        body = await request.read()
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        peer = request.transport.get_extra_info("peername") if request.transport else None
        payload = {
            "service": service_name,
            "peer": list(peer[:2]) if peer else None,  # 커넥션 재사용 확인용 (프록시 측 소켓 주소)
            "method": request.method,
            "path": request.path,
            "raw_path": request.raw_path,
            "query_string": request.query_string,
            "headers": [[k, v] for k, v in request.headers.items()],
            "body_length": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_text": text if text is not None and len(body) <= 65536 else None,
            "body_base64": base64.b64encode(body).decode() if text is None and len(body) <= 65536 else None,
        }
        try:
            payload["json"] = json.loads(body) if body else None
        except ValueError:
            payload["json"] = None
        return web.json_response(payload, headers={"X-Backend": service_name})

    @routes.post("/upload")
    @routes.put("/upload")
    async def upload(request: web.Request) -> web.Response:
        digest = hashlib.sha256()
        size = 0
        async for chunk in request.content.iter_chunked(65536):
            digest.update(chunk)
            size += len(chunk)
        return web.json_response(
            {
                "bytes": size,
                "sha256": digest.hexdigest(),
                "content_length": request.headers.get("Content-Length"),
                "transfer_encoding": request.headers.get("Transfer-Encoding"),
            }
        )

    @routes.get("/download")
    async def download(request: web.Request) -> web.StreamResponse:
        size = _int(request, "size", 1024 * 1024)
        chunked = request.query.get("chunked", "0") == "1"
        response = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
        if not chunked:
            response.content_length = size
        await response.prepare(request)
        if request.method != "HEAD":  # aiohttp StreamResponse 는 HEAD 에도 본문을 써 버린다
            for chunk in pattern_bytes(size):
                await response.write(chunk)
        await response.write_eof()
        return response

    @routes.get("/sse")
    async def sse(request: web.Request) -> web.StreamResponse:
        count = _int(request, "count", 5)
        interval = _float(request, "interval", 0.5)
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        for n in range(1, count + 1):
            data = json.dumps({"n": n, "sent_at": time.time()})
            await response.write(f"id: {n}\nevent: tick\ndata: {data}\n\n".encode())
            if n < count:
                await asyncio.sleep(interval)
        await response.write_eof()
        return response

    @routes.get("/abort")
    async def abort(request: web.Request) -> web.StreamResponse:
        """본문 일부만 보낸 뒤 연결을 끊는다 (백엔드 장애 시뮬레이션)."""
        response = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
        if request.query.get("chunked", "0") != "1":
            response.content_length = 1_000_000
        await response.prepare(request)
        await response.write(b"x" * 1000)
        await asyncio.sleep(0.05)
        if request.transport is not None:
            request.transport.abort()
        return response

    @routes.get("/slow")
    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(_float(request, "delay", 3.0))
        return web.json_response({"service": service_name, "slow": True})

    @routes.get(r"/status/{code:\d{3}}")
    async def status(request: web.Request) -> web.Response:
        code = int(request.match_info["code"])
        if code in (204, 304):
            return web.Response(status=code)
        return web.json_response({"status": code}, status=code)

    @routes.get("/cookies")
    async def cookies(request: web.Request) -> web.Response:
        response = web.json_response({"cookies": dict(request.cookies)})
        response.set_cookie("session", "abc123", httponly=True, path="/")
        response.set_cookie("theme", "dark", path="/")
        return response

    @routes.get("/redirect")
    async def redirect(request: web.Request) -> web.Response:
        raise web.HTTPFound("/echo?redirected=1")

    @routes.get("/gzip")
    async def gzipped(request: web.Request) -> web.Response:
        body = gzip.compress(("compressed payload " * 200).encode())
        return web.Response(
            body=body,
            headers={"Content-Type": "text/plain; charset=utf-8", "Content-Encoding": "gzip"},
        )

    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.add_routes(routes)
    app.router.add_route("*", "/{tail:.*}", echo)  # 그 밖의 모든 경로도 echo (예: /v1/items)
    return app


if __name__ == "__main__":
    web.run_app(
        create_app(os.environ.get("SERVICE_NAME", "test-app")),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "1000")),
        handler_cancellation=True,
    )
