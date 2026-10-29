"""도메인 → 백엔드 라우팅 테이블 (메모리 캐시).

요청마다 SQLite 를 조회하지 않도록 활성화된 라우팅만 dict 로 들고 있다.
설정이 바뀌면 RouteService 가 DB 커밋 직후 새 스냅샷으로 통째로 교체한다.
교체는 참조 할당 한 번이므로 요청 처리 중에도 항상 일관된 스냅샷을 본다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType

from ..database.models import Route

log = logging.getLogger(__name__)


class RouteTable:
    def __init__(self) -> None:
        self._routes: Mapping[str, Route] = MappingProxyType({})

    def replace(self, routes: Iterable[Route], is_target_allowed: Callable[[str], bool]) -> None:
        active: dict[str, Route] = {}
        for route in routes:
            if not route.enabled:
                continue
            if not is_target_allowed(route.target_service):
                log.warning(
                    "route %s -> %s skipped: target is not allowed by policy",
                    route.domain,
                    route.target,
                )
                continue
            active[route.domain.lower()] = route
        self._routes = MappingProxyType(active)

    def match(self, hostname: str) -> Route | None:
        """정확히 일치하는 활성 라우팅만 반환한다 (하위 도메인 자동 매칭 없음)."""
        return self._routes.get(hostname)

    def __len__(self) -> int:
        return len(self._routes)
