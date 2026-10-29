"""순수 함수 단위 테스트: Host 파싱, 입력 검증, 헤더 처리, 설정, SSRF resolver."""

from __future__ import annotations

import socket

import pytest
from multidict import CIMultiDict

from app.config import ConfigError, Settings
from app.net import is_ip_literal, parse_host_header, parse_networks, resolve_client_ip
from app.proxy.headers import ForwardingInfo, build_downstream_headers, build_upstream_headers
from app.proxy.resolver import RestrictedResolver, TargetNotAllowedError
from app.validation import (
    TargetPolicy,
    ValidationError,
    normalize_domain,
    normalize_service_name,
    parse_port,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("test.com", ("test.com", None)),
        ("TEST.com:8080", ("test.com", 8080)),
        ("test.com.", ("test.com", None)),
        ("192.168.0.10", ("192.168.0.10", None)),
        ("192.168.0.10:80", ("192.168.0.10", 80)),
        ("[::1]", ("::1", None)),
        ("[2001:db8::1]:8080", ("2001:db8::1", 8080)),
    ],
)
def test_parse_host_header(raw, expected):
    info = parse_host_header(raw)
    assert info is not None
    assert (info.hostname, info.port) == expected


@pytest.mark.parametrize("raw", ["", " ", ":80", "test.com:abc", "test.com:0", "test.com:70000", "::1", "[::1", "[test]:80", "a/b", "user@host"])
def test_parse_host_header_rejects_invalid(raw):
    assert parse_host_header(raw) is None


def test_is_ip_literal():
    assert is_ip_literal("10.0.0.1")
    assert is_ip_literal("::1")
    assert not is_ip_literal("test.com")
    assert not is_ip_literal("1.2.3")


def test_normalize_domain():
    assert normalize_domain(" Test.COM. ") == "test.com"
    assert normalize_domain("한국.kr") == "xn--3e0b707e.kr"
    for bad in ["", "*.test.com", "http://test.com", "test.com:80", "10.0.0.1", "-bad.com", "a..b", "x" * 64 + ".com", "test.123"]:
        with pytest.raises(ValidationError):
            normalize_domain(bad)


def test_normalize_service_name():
    assert normalize_service_name(" Test-App ") == "test-app"
    assert normalize_service_name("myproject_web_1") == "myproject_web_1"
    for bad in ["", "8.8.8.8", "1234", "0x7f000001", "api.example.com", "http://x", "x:80", "-app", "a" * 64]:
        with pytest.raises(ValidationError):
            normalize_service_name(bad)


def test_parse_port():
    assert parse_port("1000") == 1000
    for bad in ["0", "65536", "-1", "abc", ""]:
        with pytest.raises(ValidationError):
            parse_port(bad)


def test_target_policy():
    policy = TargetPolicy(allowed={"test-app"}, blocked={"localhost"})
    assert policy.allows("test-app")
    assert not policy.allows("localhost")
    assert not policy.allows("other")
    assert TargetPolicy(blocked={"localhost"}).allows("other")


def test_resolve_client_ip():
    trusted = parse_networks("10.0.0.0/8")
    assert resolve_client_ip("203.0.113.5", ["1.1.1.1"], trusted) == "203.0.113.5"
    assert resolve_client_ip("10.0.0.2", ["198.51.100.1, 10.0.0.9"], trusted) == "198.51.100.1"
    assert resolve_client_ip("10.0.0.2", [], trusted) == "10.0.0.2"
    assert resolve_client_ip("10.0.0.2", ["garbage"], trusted) == "10.0.0.2"


def test_build_upstream_headers_untrusted():
    incoming = CIMultiDict(
        [
            ("Host", "test.com"),
            ("Connection", "keep-alive, X-Hop"),
            ("X-Hop", "1"),
            ("Transfer-Encoding", "chunked"),
            ("Content-Length", "10"),
            ("X-Forwarded-For", "6.6.6.6"),
            ("Mcp-Session-Id", "abc"),
            ("Cookie", "a=1"),
            ("Cookie", "b=2"),
        ]
    )
    info = ForwardingInfo(peer_ip="172.18.0.1", client_ip="172.18.0.1", peer_trusted=False, host="test.com")
    out = build_upstream_headers(incoming, info, upstream_host="test.com", via="1.1 rp")
    assert out["Host"] == "test.com"
    assert out["X-Forwarded-For"] == "172.18.0.1"
    assert out["Mcp-Session-Id"] == "abc"
    assert out.getall("Cookie") == ["a=1", "b=2"]
    for name in ("Connection", "X-Hop", "Transfer-Encoding", "Content-Length"):
        assert name not in out
    assert out["Via"] == "1.1 rp"


def test_build_downstream_headers():
    upstream = CIMultiDict(
        [
            ("Content-Type", "text/event-stream"),
            ("Transfer-Encoding", "chunked"),
            ("Connection", "close"),
            ("Keep-Alive", "timeout=5"),
            ("Set-Cookie", "a=1"),
            ("Set-Cookie", "b=2"),
            ("Mcp-Session-Id", "s1"),
        ]
    )
    out = build_downstream_headers(upstream)
    assert out["Content-Type"] == "text/event-stream"
    assert out.getall("Set-Cookie") == ["a=1", "b=2"]
    assert out["Mcp-Session-Id"] == "s1"
    for name in ("Transfer-Encoding", "Connection", "Keep-Alive"):
        assert name not in out


def test_settings_require_secrets():
    with pytest.raises(ConfigError):
        Settings.from_env({})
    with pytest.raises(ConfigError):
        Settings.from_env({"ADMIN_PASSWORD": "long-enough-pw", "SESSION_SECRET": "short"})
    settings = Settings.from_env(
        {
            "ADMIN_PASSWORD": "long-enough-pw",
            "SESSION_SECRET": "s" * 40,
            "ADMIN_ALLOWED_IPS": "*",
            "TRUSTED_PROXIES": "",
            "PROXY_READ_TIMEOUT": "0",
        }
    )
    assert settings.read_timeout == 0
    assert settings.trusted_proxies == ()
    assert len(settings.admin_allowed_networks) == 2
    with pytest.raises(ConfigError):
        Settings.from_env({"ADMIN_PASSWORD": "long-enough-pw", "SESSION_SECRET": "s" * 40, "ADMIN_ALLOWED_IPS": "nope"})


class _StaticResolver:
    def __init__(self, addresses):
        self._addresses = addresses

    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [
            {"hostname": host, "host": a, "port": port, "family": family, "proto": 0, "flags": 0}
            for a in self._addresses
        ]

    async def close(self):
        pass


async def test_restricted_resolver_blocks_public_addresses():
    private = parse_networks("10.0.0.0/8,172.16.0.0/12,192.168.0.0/16")
    resolver = RestrictedResolver(private, _StaticResolver(["8.8.8.8"]))
    with pytest.raises(TargetNotAllowedError):
        await resolver.resolve("test-app", 1000)

    resolver = RestrictedResolver(private, _StaticResolver(["169.254.169.254", "172.18.0.5"]))
    results = await resolver.resolve("test-app", 1000)
    assert [r["host"] for r in results] == ["172.18.0.5"]
