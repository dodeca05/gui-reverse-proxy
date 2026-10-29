"""관리자 페이지 (9~11, 16, 17번 항목): 로그인, 라우팅 CRUD, 즉시 반영, IP/도메인 접근 분기."""

from __future__ import annotations

import aiohttp

from app.net import parse_networks

from .conftest import (
    ADMIN_PASSWORD,
    SESSION_SECRET,
    AdminClient,
    ProxyServer,
    Server,
    route_id_for,
)

# 16. IP 직접 접속 → 관리자 페이지 ---------------------------------------------------


async def test_ip_access_shows_admin_login(proxy: ProxyServer, http: aiohttp.ClientSession):
    async with http.get(proxy.url("/"), allow_redirects=False) as resp:
        assert resp.status == 303
        assert resp.headers["Location"] == "/login"
    async with http.get(proxy.url("/login")) as resp:
        assert resp.status == 200
        html = await resp.text()
        assert 'action="/login"' in html
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    async with http.get(proxy.url("/static/admin.css")) as resp:
        assert resp.status == 200


async def test_ipv6_host_is_treated_as_admin(proxy: ProxyServer, http: aiohttp.ClientSession):
    async with http.get(proxy.url("/login"), headers={"Host": "[::1]:8080"}) as resp:
        assert resp.status == 200
        assert 'action="/login"' in await resp.text()


# 17. 도메인 접속 → 관리자 페이지 노출 금지 -------------------------------------------


async def test_domain_access_never_reaches_admin(proxy: ProxyServer, backend: Server, http: aiohttp.ClientSession):
    for path in ("/", "/login", "/static/admin.css", "/routes/1/edit", "/healthz"):
        async with http.get(proxy.url(path), headers={"Host": "unrouted.example"}) as resp:
            assert resp.status == 404, path
            assert "로그인" not in await resp.text()

    # 라우팅된 도메인이면 같은 경로도 백엔드로 전달된다
    await proxy.add_route("test.com", backend.port)
    async with http.get(proxy.url("/login"), headers={"Host": "test.com"}) as resp:
        echo = await resp.json()  # 관리자 로그인 화면이 아니라 백엔드의 echo 응답
    assert echo["service"] == "test-app" and echo["path"] == "/login"
    async with http.post(proxy.url("/echo/login"), headers={"Host": "test.com"}, data={"username": "admin"}) as resp:
        assert (await resp.json())["service"] == "test-app"


# 9. 로그인 ----------------------------------------------------------------------


async def test_login_and_logout(admin: AdminClient):
    resp = await admin.login()
    assert resp.status == 303
    assert resp.headers["Location"] == "/"
    cookie = resp.headers["Set-Cookie"] if "rp_admin_session" in resp.headers.get("Set-Cookie", "") else next(
        v for v in resp.headers.getall("Set-Cookie") if v.startswith("rp_admin_session=")
    )
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie

    html = await admin.dashboard()
    assert "라우팅 목록" in html
    # 비밀번호와 세션 서명 키는 화면에 노출되지 않는다
    assert ADMIN_PASSWORD not in html
    assert SESSION_SECRET not in html

    status, _ = await admin.post("/logout")
    assert status == 303
    async with admin.session.get(admin.url("/"), allow_redirects=False) as resp:
        assert resp.status == 303
        assert resp.headers["Location"] == "/login"


async def test_login_failure(admin: AdminClient):
    resp = await admin.login(password="wrong-password")
    assert resp.status == 401
    resp = await admin.login(username="root", password=ADMIN_PASSWORD)
    assert resp.status == 401
    async with admin.session.get(admin.url("/"), allow_redirects=False) as r:
        assert r.status == 303


async def test_login_rate_limit(admin: AdminClient):
    for _ in range(3):  # login_max_attempts=3
        assert (await admin.login(password="nope")).status == 401
    resp = await admin.login()  # 올바른 비밀번호여도 잠금 중
    assert resp.status == 429
    assert int(resp.headers["Retry-After"]) > 0


async def test_login_requires_csrf_token(proxy: ProxyServer, http: aiohttp.ClientSession):
    async with http.post(
        proxy.url("/login"), data={"username": "admin", "password": ADMIN_PASSWORD}, allow_redirects=False
    ) as resp:
        assert resp.status == 403


async def test_settings_require_login(proxy: ProxyServer, http: aiohttp.ClientSession):
    async with http.get(proxy.url("/routes/1/edit"), allow_redirects=False) as resp:
        assert resp.status == 303
    for path in ("/routes", "/routes/1", "/routes/1/delete", "/routes/1/toggle"):
        async with http.post(proxy.url(path), data={"domain": "x.com"}, allow_redirects=False) as resp:
            assert resp.status == 401, path


async def test_csrf_protection_on_route_changes(admin: AdminClient):
    await admin.login()
    data = {"domain": "csrf.test.com", "target_service": "test-app", "target_port": "1000", "enabled": "on"}
    status, _ = await admin.post("/routes", data, csrf=False)
    assert status == 403
    status, _ = await admin.post("/routes", {**data, "csrf_token": "forged"}, csrf=False)
    assert status == 403

    token = await admin.csrf_token()
    async with admin.session.post(
        admin.url("/routes"),
        data={**data, "csrf_token": token},
        headers={"Origin": "http://evil.example"},
        allow_redirects=False,
    ) as resp:
        assert resp.status == 403
    assert "csrf.test.com" not in await admin.dashboard()


async def test_admin_ip_allowlist(proxy_factory, http: aiohttp.ClientSession):
    proxy = await proxy_factory(admin_allowed_networks=parse_networks("10.0.0.0/8"))
    async with http.get(proxy.url("/login")) as resp:
        assert resp.status == 403
    async with http.get(proxy.url("/healthz")) as resp:  # 헬스체크는 예외
        assert resp.status == 200
        assert await resp.json() == {"status": "ok"}


# 10, 11. 라우팅 생성 / 수정 / 삭제 및 즉시 반영 ------------------------------------------


async def test_route_crud_applies_immediately(
    admin: AdminClient, proxy: ProxyServer, backend_factory, http: aiohttp.ClientSession
):
    first = await backend_factory("test-app", name="first")
    second = await backend_factory("test-app", name="second")
    host = {"Host": "app.test.com"}
    await admin.login()

    async def service_name() -> str | int:
        async with http.get(proxy.url("/"), headers=host) as resp:
            return (await resp.json())["service"] if resp.status == 200 else resp.status

    assert await service_name() == 404

    # 생성
    status, _ = await admin.create_route("App.Test.com", "localhost", first.port)
    assert status == 303
    html = await admin.dashboard()
    assert '<span class="domain">app.test.com</span>' in html and str(first.port) in html
    assert await service_name() == "first"

    # 수정 (서비스 포트 변경) → 재시작 없이 즉시 반영
    route_id = await route_id_for(proxy, "app.test.com")
    status, _ = await admin.post(
        f"/routes/{route_id}",
        {"domain": "app.test.com", "target_service": "localhost", "target_port": str(second.port), "enabled": "on", "preserve_host": "on"},
    )
    assert status == 303
    assert await service_name() == "second"

    # 비활성화 / 활성화
    assert (await admin.post(f"/routes/{route_id}/toggle"))[0] == 303
    assert await service_name() == 404
    assert (await admin.post(f"/routes/{route_id}/toggle"))[0] == 303
    assert await service_name() == "second"

    # 삭제
    assert (await admin.post(f"/routes/{route_id}/delete"))[0] == 303
    assert await service_name() == 404
    assert '<span class="domain">app.test.com</span>' not in await admin.dashboard()


async def test_edit_page(admin: AdminClient, proxy: ProxyServer):
    await admin.login()
    await admin.create_route("edit.test.com", "test-app", 1000)
    route_id = await route_id_for(proxy, "edit.test.com")
    async with admin.session.get(admin.url(f"/routes/{route_id}/edit")) as resp:
        assert resp.status == 200
        html = await resp.text()
    assert 'value="edit.test.com"' in html and 'value="test-app"' in html
    async with admin.session.get(admin.url("/routes/99999/edit")) as resp:
        assert resp.status == 404


async def test_invalid_route_input_returns_400(admin: AdminClient, proxy_factory):
    await admin.login()
    cases = [
        ("", "test-app", "1000"),
        ("http://test.com/path", "test-app", "1000"),
        ("*.test.com", "test-app", "1000"),
        ("192.168.0.10", "test-app", "1000"),
        ("bad_domain!.com", "test-app", "1000"),
        ("ok.test.com", "", "1000"),
        ("ok.test.com", "8.8.8.8", "1000"),
        ("ok.test.com", "2130706433", "1000"),  # 정수 표기 IP
        ("ok.test.com", "evil.example.com", "1000"),  # 외부 도메인
        ("ok.test.com", "http://test-app", "1000"),
        ("ok.test.com", "test-app", "0"),
        ("ok.test.com", "test-app", "65536"),
        ("ok.test.com", "test-app", "abc"),
    ]
    for domain, service, port in cases:
        status, html = await admin.post(
            "/routes", {"domain": domain, "target_service": service, "target_port": port, "enabled": "on"}
        )
        assert status == 400, (domain, service, port)
        assert "field-error" in html
    assert "ok.test.com" not in await admin.dashboard()


async def test_blocked_and_unlisted_targets_rejected(proxy_factory):
    proxy = await proxy_factory(
        allowed_target_services=frozenset({"test-app"}),
        blocked_target_services=frozenset({"localhost", "reverse-proxy"}),
    )
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        admin = AdminClient(session, proxy)
        await admin.login()
        assert (await admin.create_route("a.test.com", "localhost", 80))[0] == 400
        assert (await admin.create_route("b.test.com", "reverse-proxy", 80))[0] == 400
        assert (await admin.create_route("c.test.com", "other-app", 80))[0] == 400
        assert (await admin.create_route("d.test.com", "test-app", 1000))[0] == 303


async def test_duplicate_domain_rejected(admin: AdminClient):
    await admin.login()
    assert (await admin.create_route("dup.test.com", "test-app", 1000))[0] == 303
    status, html = await admin.create_route("DUP.test.com.", "api-server", 8080)
    assert status == 400
    assert "이미 등록된 도메인" in html


async def test_update_to_existing_domain_rejected(admin: AdminClient, proxy: ProxyServer):
    await admin.login()
    await admin.create_route("one.test.com", "test-app", 1000)
    await admin.create_route("two.test.com", "test-app", 1001)
    route_id = await route_id_for(proxy, "two.test.com")
    status, _ = await admin.post(
        f"/routes/{route_id}", {"domain": "one.test.com", "target_service": "test-app", "target_port": "1001"}
    )
    assert status == 400
