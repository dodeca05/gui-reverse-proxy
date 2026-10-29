"""라우팅 설정 변경 서비스.

DB 쓰기와 메모리 라우팅 테이블 갱신을 하나의 단위로 묶어 '저장된 설정 = 실제 라우팅' 을 보장한다.
  - 모든 변경은 asyncio.Lock 으로 직렬화한다.
  - 커밋 직후 DB 에서 전체 라우팅을 다시 읽어 테이블을 교체한다.
  - 관리자 요청이 도중에 취소(클라이언트 연결 종료)되어도 커밋과 캐시 갱신은 끝까지 수행되도록
    asyncio.shield 로 보호한다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .database.models import Route, RouteInput
from .database.repository import RouteRepository
from .proxy.router import RouteTable
from .validation import TargetPolicy

log = logging.getLogger(__name__)

T = TypeVar("T")


class RouteService:
    def __init__(self, repository: RouteRepository, table: RouteTable, policy: TargetPolicy) -> None:
        self._repo = repository
        self._table = table
        self._policy = policy
        self._lock = asyncio.Lock()

    @property
    def policy(self) -> TargetPolicy:
        return self._policy

    async def load(self) -> None:
        async with self._lock:
            await self._reload()

    async def list_routes(self) -> list[Route]:
        return await self._repo.list_routes()

    async def get(self, route_id: int) -> Route | None:
        return await self._repo.get(route_id)

    async def create(self, data: RouteInput) -> Route:
        route = await self._mutate(lambda: self._repo.create(data))
        log.info("route created: %s -> %s (enabled=%s)", route.domain, route.target, route.enabled)
        return route

    async def update(self, route_id: int, data: RouteInput) -> Route:
        route = await self._mutate(lambda: self._repo.update(route_id, data))
        log.info("route updated: %s -> %s (enabled=%s)", route.domain, route.target, route.enabled)
        return route

    async def set_enabled(self, route_id: int, enabled: bool) -> Route:
        route = await self._mutate(lambda: self._repo.set_enabled(route_id, enabled))
        log.info("route %s: %s", "enabled" if enabled else "disabled", route.domain)
        return route

    async def delete(self, route_id: int) -> None:
        await self._mutate(lambda: self._repo.delete(route_id))
        log.info("route deleted: id=%s", route_id)

    async def _mutate(self, operation: Callable[[], Awaitable[T]]) -> T:
        async def run() -> T:
            async with self._lock:
                try:
                    return await operation()
                finally:
                    # 실패한 경우에도 DB 상태와 다시 맞춘다
                    await self._reload()

        return await asyncio.shield(run())

    async def _reload(self) -> None:
        routes = await self._repo.list_routes()
        self._table.replace(routes, self._policy.allows)
        log.debug("routing table reloaded: %d active routes", len(self._table))
