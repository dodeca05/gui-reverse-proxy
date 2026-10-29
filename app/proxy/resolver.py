"""SSRF 방지용 DNS resolver.

Docker 내장 DNS 로 서비스명을 해석한 뒤, 결과 IP 가 ALLOWED_TARGET_NETWORKS(기본: 사설망)에
속할 때만 연결을 허용한다. 서비스명이 외부 DNS 로 새어 나가 공인 IP/루프백/메타데이터 주소로
해석되는 경우를 차단한다.

DNS 캐시를 쓰지 않으므로(커넥터의 use_dns_cache=False) 컨테이너가 재생성되어 IP 가 바뀌어도
새 연결은 항상 최신 IP 로 향한다.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Sequence

from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver

from ..net import IPNetwork, ip_in_networks

log = logging.getLogger(__name__)


class TargetNotAllowedError(OSError):
    pass


class RestrictedResolver(AbstractResolver):
    def __init__(
        self, allowed_networks: Sequence[IPNetwork], inner: AbstractResolver | None = None
    ) -> None:
        self._allowed = tuple(allowed_networks)
        self._inner = inner or DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await self._inner.resolve(host, port, family)
        allowed = [r for r in results if ip_in_networks(r["host"], self._allowed)]
        if not allowed:
            addresses = ", ".join(sorted({r["host"] for r in results}))
            log.warning(
                "blocked upstream %s: resolved to %s outside ALLOWED_TARGET_NETWORKS",
                host,
                addresses,
            )
            raise TargetNotAllowedError(
                f"{host} resolved to {addresses}, which is not in ALLOWED_TARGET_NETWORKS"
            )
        return allowed

    async def close(self) -> None:
        await self._inner.close()
