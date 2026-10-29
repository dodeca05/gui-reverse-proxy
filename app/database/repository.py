"""aiosqlite 기반 라우팅 저장소."""

from __future__ import annotations

import logging
import os
import sqlite3

import aiosqlite

from .models import ROUTE_COLUMNS, SCHEMA, SCHEMA_VERSION, Route, RouteInput, utcnow_iso

log = logging.getLogger(__name__)


class DuplicateDomainError(Exception):
    def __init__(self, domain: str) -> None:
        super().__init__(f"domain already registered: {domain}")
        self.domain = domain


class RouteNotFoundError(Exception):
    pass


class RouteRepository:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("repository is not open")
        return self._db

    async def open(self) -> None:
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=FULL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._migrate()
        log.info("database opened: %s", self._path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _migrate(self) -> None:
        async with self.db.execute("PRAGMA user_version") as cur:
            (version,) = await cur.fetchone()
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {version} is newer than supported {SCHEMA_VERSION}"
            )
        await self.db.executescript(SCHEMA)
        await self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        await self.db.commit()

    async def ping(self) -> None:
        async with self.db.execute("SELECT 1") as cur:
            await cur.fetchone()

    async def list_routes(self) -> list[Route]:
        async with self.db.execute(
            f"SELECT {ROUTE_COLUMNS} FROM routes ORDER BY domain"
        ) as cur:
            return [Route.from_row(tuple(row)) for row in await cur.fetchall()]

    async def get(self, route_id: int) -> Route | None:
        async with self.db.execute(
            f"SELECT {ROUTE_COLUMNS} FROM routes WHERE id = ?", (route_id,)
        ) as cur:
            row = await cur.fetchone()
        return Route.from_row(tuple(row)) if row else None

    async def create(self, data: RouteInput) -> Route:
        now = utcnow_iso()
        try:
            cur = await self.db.execute(
                "INSERT INTO routes (domain, target_service, target_port, preserve_host, enabled,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    data.domain,
                    data.target_service,
                    data.target_port,
                    int(data.preserve_host),
                    int(data.enabled),
                    now,
                    now,
                ),
            )
            await self.db.commit()
        except sqlite3.IntegrityError as exc:
            await self.db.rollback()
            raise DuplicateDomainError(data.domain) from exc
        route = await self.get(cur.lastrowid)
        assert route is not None
        return route

    async def update(self, route_id: int, data: RouteInput) -> Route:
        try:
            cur = await self.db.execute(
                "UPDATE routes SET domain = ?, target_service = ?, target_port = ?,"
                " preserve_host = ?, enabled = ?, updated_at = ? WHERE id = ?",
                (
                    data.domain,
                    data.target_service,
                    data.target_port,
                    int(data.preserve_host),
                    int(data.enabled),
                    utcnow_iso(),
                    route_id,
                ),
            )
            await self.db.commit()
        except sqlite3.IntegrityError as exc:
            await self.db.rollback()
            raise DuplicateDomainError(data.domain) from exc
        if cur.rowcount == 0:
            raise RouteNotFoundError(route_id)
        route = await self.get(route_id)
        assert route is not None
        return route

    async def set_enabled(self, route_id: int, enabled: bool) -> Route:
        cur = await self.db.execute(
            "UPDATE routes SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), utcnow_iso(), route_id),
        )
        await self.db.commit()
        if cur.rowcount == 0:
            raise RouteNotFoundError(route_id)
        route = await self.get(route_id)
        assert route is not None
        return route

    async def delete(self, route_id: int) -> None:
        cur = await self.db.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        await self.db.commit()
        if cur.rowcount == 0:
            raise RouteNotFoundError(route_id)
