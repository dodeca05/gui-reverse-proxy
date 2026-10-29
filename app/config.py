"""환경변수 기반 설정.

관리자 비밀번호와 세션 서명 키는 소스코드에 기본값을 두지 않고 반드시 환경변수로 받는다.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .net import IPNetwork, parse_networks

PRIVATE_NETWORKS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
LOOPBACK_NETWORKS = "127.0.0.0/8,::1/128"

MIN_PASSWORD_LENGTH = 8
MIN_SECRET_LENGTH = 32


class ConfigError(Exception):
    """잘못되었거나 누락된 설정."""


@dataclass(frozen=True, slots=True)
class Settings:
    admin_password: str
    session_secret: str
    admin_username: str = "admin"

    listen_host: str = "0.0.0.0"
    listen_port: int = 80
    database_path: str = "/app/data/proxy.db"
    log_level: str = "INFO"

    # 관리자 페이지 보호
    admin_allowed_networks: tuple[IPNetwork, ...] = field(
        default_factory=lambda: parse_networks(f"{LOOPBACK_NETWORKS},{PRIVATE_NETWORKS}")
    )
    session_ttl: int = 8 * 3600
    login_max_attempts: int = 5
    login_lockout_seconds: int = 300

    # X-Forwarded-* 를 신뢰할 앞단 프록시 (비어 있으면 아무도 신뢰하지 않음)
    trusted_proxies: tuple[IPNetwork, ...] = ()

    # 라우팅 대상 제한 (SSRF / 오픈 프록시 방지)
    allowed_target_services: frozenset[str] = frozenset()  # 비어 있으면 형식만 검사
    blocked_target_services: frozenset[str] = frozenset({"localhost", "reverse-proxy"})
    allowed_target_networks: tuple[IPNetwork, ...] = field(
        default_factory=lambda: parse_networks(PRIVATE_NETWORKS)
    )

    # 업스트림 연결 (0 이면 해당 타임아웃 비활성화)
    connect_timeout: float = 5.0
    read_timeout: float = 60.0
    upstream_keepalive_timeout: float = 4.0
    upstream_pool_limit: int = 1000
    shutdown_timeout: float = 5.0

    def validate(self) -> None:
        if not self.admin_username:
            raise ConfigError("ADMIN_USERNAME must not be empty")
        if len(self.admin_password) < MIN_PASSWORD_LENGTH:
            raise ConfigError(
                f"ADMIN_PASSWORD is required and must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        if len(self.session_secret) < MIN_SECRET_LENGTH:
            raise ConfigError(
                f"SESSION_SECRET is required and must be at least {MIN_SECRET_LENGTH} characters "
                '(e.g. python -c "import secrets; print(secrets.token_urlsafe(48))")'
            )
        if not 1 <= self.listen_port <= 65535:
            raise ConfigError("LISTEN_PORT must be between 1 and 65535")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """환경변수를 읽어 Settings를 만든다. 비어 있는 값은 기본값으로 취급한다."""
        env = os.environ if env is None else env
        read = _EnvReader(env)
        settings = cls(
            admin_username=read.text("ADMIN_USERNAME", _default("admin_username")),
            admin_password=read.text("ADMIN_PASSWORD", "", strip=False),
            session_secret=read.text("SESSION_SECRET", "", strip=False),
            listen_host=read.text("LISTEN_HOST", _default("listen_host")),
            listen_port=read.integer("LISTEN_PORT", _default("listen_port")),
            database_path=read.text("DATABASE_PATH", _default("database_path")),
            log_level=read.text("LOG_LEVEL", _default("log_level")).upper(),
            admin_allowed_networks=read.networks(
                "ADMIN_ALLOWED_IPS", _default("admin_allowed_networks")
            ),
            session_ttl=read.integer("SESSION_TTL_SECONDS", _default("session_ttl"), minimum=60),
            login_max_attempts=read.integer(
                "LOGIN_MAX_ATTEMPTS", _default("login_max_attempts"), minimum=1
            ),
            login_lockout_seconds=read.integer(
                "LOGIN_LOCKOUT_SECONDS", _default("login_lockout_seconds"), minimum=1
            ),
            trusted_proxies=read.networks("TRUSTED_PROXIES", _default("trusted_proxies")),
            allowed_target_services=read.names(
                "ALLOWED_TARGET_SERVICES", _default("allowed_target_services")
            ),
            blocked_target_services=read.names(
                "BLOCKED_TARGET_SERVICES", _default("blocked_target_services")
            ),
            allowed_target_networks=read.networks(
                "ALLOWED_TARGET_NETWORKS", _default("allowed_target_networks")
            ),
            connect_timeout=read.number("PROXY_CONNECT_TIMEOUT", _default("connect_timeout")),
            read_timeout=read.number("PROXY_READ_TIMEOUT", _default("read_timeout")),
            upstream_keepalive_timeout=read.number(
                "PROXY_KEEPALIVE_TIMEOUT", _default("upstream_keepalive_timeout")
            ),
            upstream_pool_limit=read.integer(
                "PROXY_POOL_LIMIT", _default("upstream_pool_limit"), minimum=1
            ),
            shutdown_timeout=read.number("SHUTDOWN_TIMEOUT", _default("shutdown_timeout")),
        )
        settings.validate()
        return settings


def _default(name: str) -> Any:
    f = Settings.__dataclass_fields__[name]
    if f.default_factory is not dataclasses.MISSING:
        return f.default_factory()
    return f.default


class _EnvReader:
    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

    def _raw(self, name: str) -> str | None:
        value = self._env.get(name)
        if value is None or value.strip() == "":
            return None
        return value

    def text(self, name: str, default: str, *, strip: bool = True) -> str:
        value = self._raw(name)
        if value is None:
            return default
        return value.strip() if strip else value

    def integer(self, name: str, default: int, *, minimum: int | None = None) -> int:
        value = self._raw(name)
        if value is None:
            return default
        try:
            result = int(value.strip())
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer") from exc
        if minimum is not None and result < minimum:
            raise ConfigError(f"{name} must be >= {minimum}")
        return result

    def number(self, name: str, default: float) -> float:
        value = self._raw(name)
        if value is None:
            return default
        try:
            result = float(value.strip())
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number") from exc
        if result < 0:
            raise ConfigError(f"{name} must be >= 0")
        return result

    def networks(self, name: str, default: tuple[IPNetwork, ...]) -> tuple[IPNetwork, ...]:
        value = self._raw(name)
        if value is None:
            return default
        try:
            return parse_networks(value)
        except ValueError as exc:
            raise ConfigError(f"{name}: {exc}") from exc

    def names(self, name: str, default: frozenset[str]) -> frozenset[str]:
        value = self._raw(name)
        if value is None:
            return default
        return frozenset(part.strip().lower() for part in value.split(",") if part.strip())
