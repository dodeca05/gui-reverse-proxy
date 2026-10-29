"""프록시 요청/응답 헤더 처리.

- Hop-by-hop 헤더(RFC 9110 7.6.1)와 Connection 헤더에 나열된 헤더를 제거한다.
- X-Forwarded-* 는 신뢰 프록시에서 온 경우에만 이어 붙이고, 그 외에는 새로 만든다.
- 그 밖의 end-to-end 헤더(Authorization, Cookie, Accept, MCP-* 등)는 순서와 중복을 유지해 전달한다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from multidict import CIMultiDict, CIMultiDictProxy, istr

HOP_BY_HOP_HEADERS = frozenset(
    istr(h)
    for h in (
        "Connection",
        "Keep-Alive",
        "Proxy-Connection",
        "Proxy-Authenticate",
        "Proxy-Authorization",
        "TE",
        "Trailer",
        "Transfer-Encoding",
        "Upgrade",
    )
)

# 클라이언트가 보낸 값을 그대로 믿으면 안 되는 전달 정보 헤더
FORWARDING_HEADERS = frozenset(
    istr(h)
    for h in (
        "Forwarded",
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "X-Forwarded-Port",
        "X-Real-IP",
    )
)

# 프록시가 직접 처리하므로 업스트림으로 전달하지 않는 요청 헤더
#  - Host: 라우팅 설정에 따라 다시 설정
#  - Expect: 100-continue 는 프록시가 클라이언트에 직접 응답한다
_REQUEST_MANAGED = frozenset(istr(h) for h in ("Host", "Expect"))

Headers = CIMultiDict[str] | CIMultiDictProxy[str]


def connection_tokens(headers: Headers) -> set[istr]:
    """Connection 헤더에 나열된 헤더 이름들 (hop-by-hop 으로 취급해야 함)."""
    tokens: set[istr] = set()
    for value in headers.getall("Connection", ()):
        tokens.update(istr(t.strip()) for t in value.split(",") if t.strip())
    return tokens


def _end_to_end(headers: Headers, extra_drop: Iterable[istr] = ()) -> CIMultiDict[str]:
    drop = HOP_BY_HOP_HEADERS | connection_tokens(headers) | frozenset(extra_drop)
    result: CIMultiDict[str] = CIMultiDict()
    for name, value in headers.items():
        if istr(name) not in drop:
            result.add(name, value)
    return result


@dataclass(frozen=True, slots=True)
class ForwardingInfo:
    """업스트림에 전달할 클라이언트 정보."""

    peer_ip: str  # 프록시에 직접 연결한 주소
    client_ip: str  # 신뢰 프록시 경계를 고려해 계산한 실제 클라이언트 주소
    peer_trusted: bool  # peer 가 TRUSTED_PROXIES 에 속하는지
    host: str  # 클라이언트가 보낸 원래 Host 헤더 값
    proto: str = "http"


def build_upstream_headers(
    incoming: Headers,
    forwarding: ForwardingInfo,
    *,
    upstream_host: str,
    via: str,
) -> CIMultiDict[str]:
    """백엔드로 보낼 요청 헤더를 만든다.

    ``upstream_host`` 는 백엔드에 보낼 Host 헤더 값이다 (원래 Host 또는 ``service:port``).
    """
    drop = set(_REQUEST_MANAGED) | {istr("X-Real-IP")}
    if not forwarding.peer_trusted:
        drop |= FORWARDING_HEADERS
    if "Transfer-Encoding" in incoming:
        # 본문은 스트리밍으로 다시 chunked 인코딩되므로 원래 길이 정보는 의미가 없다
        drop.add(istr("Content-Length"))

    # 신뢰 프록시가 보낸 기존 값 (connection 토큰으로 제거되기 전에 읽는다)
    prior_xff = ", ".join(incoming.getall("X-Forwarded-For", ())) if forwarding.peer_trusted else ""
    prior_via = ", ".join(incoming.getall("Via", ()))

    headers = _end_to_end(incoming, drop)
    headers.popall("Via", None)

    headers["Host"] = upstream_host
    headers["X-Forwarded-For"] = (
        f"{prior_xff}, {forwarding.peer_ip}" if prior_xff else forwarding.peer_ip
    )
    if "X-Forwarded-Host" not in headers:
        headers["X-Forwarded-Host"] = forwarding.host
    if "X-Forwarded-Proto" not in headers:
        headers["X-Forwarded-Proto"] = forwarding.proto
    headers["X-Real-IP"] = forwarding.client_ip
    headers["Via"] = f"{prior_via}, {via}" if prior_via else via
    return headers


def build_downstream_headers(upstream: Headers) -> CIMultiDict[str]:
    """클라이언트에 돌려줄 응답 헤더를 만든다.

    Content-Length / Content-Encoding 은 본문을 변환하지 않으므로 그대로 유지한다.
    Transfer-Encoding 은 hop-by-hop 이며 aiohttp 가 필요 시 chunked 로 다시 설정한다.
    """
    extra: set[istr] = set()
    if "Transfer-Encoding" in upstream:
        extra.add(istr("Content-Length"))  # 둘 다 있으면 Content-Length 는 무시해야 한다
    return _end_to_end(upstream, extra)


def has_via_token(headers: Headers, token: str) -> bool:
    """요청이 이미 이 프록시를 거쳤는지 (라우팅 루프 감지)."""
    for value in headers.getall("Via", ()):
        for hop in value.split(","):
            parts = hop.split()
            if len(parts) >= 2 and parts[1] == token:
                return True
    return False
