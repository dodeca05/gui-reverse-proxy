"""라우팅 규칙 입력값 검증과 라우팅 대상 허용 정책."""

from __future__ import annotations

import re
import socket
from collections.abc import Collection

from .net import is_ip_literal

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")
# Docker Compose 서비스명 / 컨테이너명 (단일 DNS 레이블). 점(.)을 허용하지 않아 외부 FQDN 을 막는다.
_SERVICE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$")


class ValidationError(ValueError):
    """사용자 입력 검증 실패. 메시지는 관리자 화면에 그대로 표시된다."""


def normalize_domain(value: str) -> str:
    """라우팅 도메인을 소문자 ASCII(국제화 도메인은 punycode)로 정규화한다."""
    domain = value.strip().lower().rstrip(".")
    if not domain:
        raise ValidationError("도메인을 입력하세요.")
    if "*" in domain:
        raise ValidationError("와일드카드 도메인은 지원하지 않습니다. 정확한 도메인을 입력하세요.")
    if "://" in domain or "/" in domain or ":" in domain:
        raise ValidationError("도메인에는 스킴, 포트, 경로를 포함할 수 없습니다. 예: test.com")
    if is_ip_literal(domain):
        raise ValidationError("IP 주소는 도메인으로 등록할 수 없습니다. (IP 접속은 관리자 페이지 전용)")
    if not domain.isascii():
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValidationError("올바른 도메인 형식이 아닙니다.") from exc
    if len(domain) > 253 or not _DOMAIN_RE.match(domain):
        raise ValidationError("올바른 도메인 형식이 아닙니다. 영문 소문자, 숫자, '-', '.'만 사용할 수 있습니다.")
    if domain.rsplit(".", 1)[-1].isdigit():
        raise ValidationError("최상위 도메인은 숫자로만 구성될 수 없습니다.")
    return domain


def normalize_service_name(value: str) -> str:
    """Docker 서비스명을 검증한다. IP 주소, URL, 외부 도메인은 허용하지 않는다."""
    name = value.strip().lower()
    if not name:
        raise ValidationError("Docker 서비스명을 입력하세요.")
    if "://" in name or "/" in name or ":" in name:
        raise ValidationError("서비스명에는 스킴, 포트, 경로를 넣을 수 없습니다. 예: test-app")
    if is_ip_literal(name) or _looks_like_ipv4(name):
        raise ValidationError("IP 주소는 라우팅 대상으로 사용할 수 없습니다. Docker 서비스명을 입력하세요.")
    if "." in name:
        raise ValidationError("외부 도메인은 라우팅 대상으로 사용할 수 없습니다. Docker 서비스명만 입력하세요.")
    if not _SERVICE_RE.match(name):
        raise ValidationError(
            "올바른 서비스명이 아닙니다. 영문 소문자, 숫자, '-', '_'만 사용할 수 있습니다 (최대 63자)."
        )
    return name


def parse_port(value: str | int) -> int:
    text = str(value).strip()
    if not text.isdigit():
        raise ValidationError("포트는 1~65535 사이의 숫자여야 합니다.")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValidationError("포트는 1~65535 사이의 숫자여야 합니다.")
    return port


def _looks_like_ipv4(name: str) -> bool:
    """``1234``, ``0x7f000001`` 처럼 getaddrinfo 가 IPv4 로 해석하는 축약 표기를 걸러낸다."""
    try:
        socket.inet_aton(name)
    except OSError:
        return False
    return True


class TargetPolicy:
    """라우팅 대상 서비스 허용/차단 목록."""

    def __init__(self, allowed: Collection[str] = (), blocked: Collection[str] = ()) -> None:
        self._allowed = frozenset(s.lower() for s in allowed)
        self._blocked = frozenset(s.lower() for s in blocked)

    def check(self, service: str) -> None:
        if service in self._blocked:
            raise ValidationError(f"'{service}' 는 라우팅 대상으로 사용할 수 없습니다 (차단 목록).")
        if self._allowed and service not in self._allowed:
            raise ValidationError(
                f"'{service}' 는 허용된 라우팅 대상이 아닙니다 (ALLOWED_TARGET_SERVICES)."
            )

    def allows(self, service: str) -> bool:
        try:
            self.check(service)
        except ValidationError:
            return False
        return True
