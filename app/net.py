"""Host 헤더 파싱, IP 판별, 신뢰 프록시 경계를 고려한 클라이언트 IP 계산."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from typing import NamedTuple

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

ANY_NETWORKS = ("0.0.0.0/0", "::/0")


class HostInfo(NamedTuple):
    """정규화된 Host 헤더. hostname은 소문자이며 끝의 '.'과 IPv6 대괄호가 제거된 상태."""

    hostname: str
    port: int | None
    raw: str


def parse_host_header(value: str) -> HostInfo | None:
    """Host 헤더를 hostname과 port로 분리한다. 형식이 잘못되었으면 None.

    - ``example.com:8080`` → (``example.com``, 8080)
    - ``[::1]:80`` → (``::1``, 80)
    - 대소문자는 구분하지 않으며 FQDN 끝의 '.'은 제거한다.
    """
    raw = value
    value = value.strip()
    if not value:
        return None

    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        host, rest = value[1:end], value[end + 1 :]
        if rest and not rest.startswith(":"):
            return None
        port_text = rest[1:] if rest else ""
        if not is_ip_literal(host) or ":" not in host:
            return None
    else:
        if value.count(":") > 1:  # 대괄호 없는 IPv6 는 Host 헤더로 유효하지 않다
            return None
        host, _, port_text = value.partition(":")

    port: int | None = None
    if port_text:
        if not port_text.isdigit() or len(port_text) > 5:
            return None
        port = int(port_text)
        if not 0 < port <= 65535:
            return None

    host = host.lower().rstrip(".")
    if not host or any(ch.isspace() or ch in "/\\@?#" for ch in host):
        return None
    return HostInfo(host, port, raw)


def is_ip_literal(host: str) -> bool:
    """``ipaddress`` 표준 라이브러리로 IP 주소 여부를 판별한다."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def parse_ip(value: str) -> IPAddress | None:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def parse_networks(value: str | Iterable[str]) -> tuple[IPNetwork, ...]:
    """``"10.0.0.0/8, 192.168.1.10"`` 형태를 네트워크 튜플로 변환한다. ``*`` 은 전체 허용."""
    parts = value.split(",") if isinstance(value, str) else list(value)
    networks: list[IPNetwork] = []
    for part in (p.strip() for p in parts):
        if not part:
            continue
        if part == "*":
            networks.extend(ipaddress.ip_network(n) for n in ANY_NETWORKS)
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError as exc:
            raise ValueError(f"invalid IP network {part!r}") from exc
    return tuple(networks)


def ip_in_networks(ip: str | IPAddress | None, networks: Sequence[IPNetwork]) -> bool:
    if ip is None:
        return False
    addr = parse_ip(ip) if isinstance(ip, str) else ip
    if addr is None:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr.version == net.version and addr in net for net in networks)


def split_forwarded_for(values: Iterable[str]) -> list[str]:
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def resolve_client_ip(
    peer: str | None, forwarded_for: Sequence[str], trusted_proxies: Sequence[IPNetwork]
) -> str | None:
    """실제 클라이언트 IP를 계산한다.

    직접 연결한 peer 가 신뢰 프록시일 때만 X-Forwarded-For 를 오른쪽부터 따라가며
    처음 만나는 '신뢰하지 않는 주소'를 클라이언트로 본다. 그렇지 않으면 peer 가 클라이언트다.
    """
    if not peer or not ip_in_networks(peer, trusted_proxies):
        return peer
    chain = split_forwarded_for(forwarded_for)
    for entry in reversed(chain):
        addr = parse_ip(entry)
        if addr is None:
            # 형식이 깨진 값 이후는 신뢰할 수 없으므로 마지막으로 확인된 주소를 사용
            return peer
        if not ip_in_networks(addr, trusted_proxies):
            return str(addr)
        peer = str(addr)
    return peer
