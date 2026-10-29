"""관리자 인증: 자격 증명 확인, 서명된 세션 쿠키, CSRF, 로그인 시도 제한.

세션은 서버 메모리에 저장하고 쿠키에는 ``<세션ID>.<HMAC 서명>`` 만 담는다.
서명 키(SESSION_SECRET)는 환경변수로만 받으며 로그아웃 시 서버 측 세션이 즉시 폐기된다.
프로세스가 재시작되면 모든 관리자 세션이 만료된다(다시 로그인 필요).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from aiohttp import web

SESSION_COOKIE = "rp_admin_session"
LOGIN_CSRF_COOKIE = "rp_login_csrf"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_match(expected: str | None, received: str | None) -> bool:
    if not expected or not received:
        return False
    return hmac.compare_digest(expected.encode(), received.encode())


def verify_credentials(
    expected_username: str, expected_password: str, username: str, password: str
) -> bool:
    """타이밍 공격을 피하기 위해 길이와 무관하게 상수 시간으로 비교한다."""
    user_ok = hmac.compare_digest(
        hashlib.sha256(username.encode()).digest(), hashlib.sha256(expected_username.encode()).digest()
    )
    pass_ok = hmac.compare_digest(
        hashlib.sha256(password.encode()).digest(), hashlib.sha256(expected_password.encode()).digest()
    )
    return user_ok & pass_ok


@dataclass(slots=True)
class Session:
    id: str
    username: str
    csrf_token: str
    expires_at: float
    flashes: list[tuple[str, str]] = field(default_factory=list)

    def flash(self, category: str, message: str) -> None:
        self.flashes.append((category, message))

    def pop_flashes(self) -> list[tuple[str, str]]:
        messages, self.flashes = self.flashes, []
        return messages


class SessionManager:
    def __init__(self, secret: str, ttl: int, *, max_sessions: int = 1000) -> None:
        self._key = hashlib.sha256(b"rp-session:" + secret.encode()).digest()
        self._ttl = ttl
        self._max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}

    @property
    def ttl(self) -> int:
        return self._ttl

    def _sign(self, value: str) -> str:
        mac = hmac.new(self._key, value.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()

    def create(self, username: str) -> Session:
        self._purge()
        if len(self._sessions) >= self._max_sessions:
            oldest = min(self._sessions.values(), key=lambda s: s.expires_at)
            self._sessions.pop(oldest.id, None)
        session = Session(
            id=secrets.token_urlsafe(32),
            username=username,
            csrf_token=new_token(),
            expires_at=time.monotonic() + self._ttl,
        )
        self._sessions[session.id] = session
        return session

    def cookie_value(self, session: Session) -> str:
        return f"{session.id}.{self._sign(session.id)}"

    def load(self, cookie_value: str | None) -> Session | None:
        if not cookie_value or "." not in cookie_value:
            return None
        session_id, signature = cookie_value.rsplit(".", 1)
        if not tokens_match(self._sign(session_id), signature):
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.expires_at <= time.monotonic():
            self._sessions.pop(session_id, None)
            return None
        return session

    def destroy(self, session: Session) -> None:
        self._sessions.pop(session.id, None)

    def _purge(self) -> None:
        now = time.monotonic()
        for sid in [sid for sid, s in self._sessions.items() if s.expires_at <= now]:
            del self._sessions[sid]


class LoginRateLimiter:
    """IP 별 로그인 실패 횟수를 세고, 한도를 넘으면 일정 시간 로그인을 막는다."""

    def __init__(self, max_attempts: int, lockout_seconds: int, *, max_entries: int = 10_000) -> None:
        self._max_attempts = max_attempts
        self._lockout = lockout_seconds
        self._max_entries = max_entries
        # ip -> (연속 실패 횟수, 마지막 실패 시각, 잠금 해제 시각)
        self._entries: dict[str, tuple[int, float, float]] = {}

    def retry_after(self, ip: str) -> int:
        """잠겨 있으면 남은 초, 아니면 0."""
        entry = self._entries.get(ip)
        if entry is None:
            return 0
        remaining = entry[2] - time.monotonic()
        return max(0, int(remaining + 0.999))

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        count, last, locked_until = self._entries.get(ip, (0, now, 0.0))
        if now - last > self._lockout:
            count = 0  # 오래된 실패 기록은 초기화
        count += 1
        if count >= self._max_attempts:
            self._entries[ip] = (0, now, now + self._lockout)
        else:
            self._entries[ip] = (count, now, locked_until)
        self._prune(now)

    def reset(self, ip: str) -> None:
        self._entries.pop(ip, None)

    def _prune(self, now: float) -> None:
        if len(self._entries) <= self._max_entries:
            return
        expired = [
            ip
            for ip, (_, last, locked_until) in self._entries.items()
            if locked_until <= now and now - last > self._lockout
        ]
        for ip in expired:
            del self._entries[ip]


def same_origin(request: web.Request) -> bool:
    """Origin(없으면 Referer) 헤더가 있다면 요청 Host 와 같은 출처인지 확인한다."""
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return True  # 헤더가 없는 클라이언트는 CSRF 토큰 검사에 맡긴다
    if source == "null":
        return False
    try:
        netloc = urlsplit(source).netloc
    except ValueError:
        return False
    return netloc.lower() == request.headers.get("Host", "").strip().lower()
