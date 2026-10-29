"""SQLite 영구 저장 (12번 항목)."""

from __future__ import annotations

import sqlite3

import aiohttp

from app.database.backup import backup

from .conftest import AdminClient, ProxyServer, Server, make_settings


async def test_routes_survive_restart(tmp_path, backend: Server, http: aiohttp.ClientSession):
    settings = make_settings(tmp_path)

    first = ProxyServer(settings)
    await first.start()
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
            admin = AdminClient(session, first)
            await admin.login()
            assert (await admin.create_route("persist.test.com", "localhost", backend.port))[0] == 303
            assert (await admin.create_route("disabled.test.com", "localhost", backend.port, enabled=False))[0] == 303
    finally:
        await first.stop()

    # 같은 DB 파일로 새 프로세스(앱)를 시작
    second = ProxyServer(settings)
    await second.start()
    try:
        async with http.get(second.url("/"), headers={"Host": "persist.test.com"}) as resp:
            assert resp.status == 200
        async with http.get(second.url("/"), headers={"Host": "disabled.test.com"}) as resp:
            assert resp.status == 404
    finally:
        await second.stop()

    with sqlite3.connect(settings.database_path) as db:
        rows = db.execute("SELECT domain, target_service, enabled FROM routes ORDER BY domain").fetchall()
    assert rows == [("disabled.test.com", "localhost", 0), ("persist.test.com", "localhost", 1)]


async def test_online_backup(tmp_path, proxy: ProxyServer):
    await proxy.add_route("backup.test.com", 1000, service="test-app")
    destination = tmp_path / "backup.db"
    backup(proxy.settings.database_path, str(destination))
    with sqlite3.connect(destination) as db:
        assert db.execute("SELECT domain FROM routes").fetchall() == [("backup.test.com",)]
