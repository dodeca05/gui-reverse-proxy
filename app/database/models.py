"""라우팅 규칙 모델과 SQLite 스키마."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    target_service  TEXT    NOT NULL,
    target_port     INTEGER NOT NULL CHECK (target_port BETWEEN 1 AND 65535),
    preserve_host   INTEGER NOT NULL DEFAULT 1 CHECK (preserve_host IN (0, 1)),
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
"""

ROUTE_COLUMNS = (
    "id, domain, target_service, target_port, preserve_host, enabled, created_at, updated_at"
)


@dataclass(frozen=True, slots=True)
class RouteInput:
    """검증을 통과한 라우팅 입력값."""

    domain: str
    target_service: str
    target_port: int
    enabled: bool = True
    preserve_host: bool = True


@dataclass(frozen=True, slots=True)
class Route:
    id: int
    domain: str
    target_service: str
    target_port: int
    preserve_host: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @property
    def target(self) -> str:
        return f"{self.target_service}:{self.target_port}"

    @classmethod
    def from_row(cls, row: tuple) -> Route:
        id_, domain, service, port, preserve_host, enabled, created, updated = row
        return cls(
            id=id_,
            domain=domain,
            target_service=service,
            target_port=port,
            preserve_host=bool(preserve_host),
            enabled=bool(enabled),
            created_at=datetime.fromisoformat(created),
            updated_at=datetime.fromisoformat(updated),
        )


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
