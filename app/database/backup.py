"""SQLite 온라인 백업 도구.

실행 중에도 일관된 스냅샷을 만들기 위해 파일 복사 대신 SQLite backup API 를 사용한다.

    python -m app.database.backup                       # /app/data/backup-<시각>.db
    python -m app.database.backup /app/data/manual.db   # 지정 경로
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime


def backup(source: str, destination: str) -> None:
    src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def main(argv: list[str]) -> int:
    source = os.environ.get("DATABASE_PATH") or "/app/data/proxy.db"
    if not os.path.exists(source):
        print(f"database not found: {source}", file=sys.stderr)
        return 1
    if len(argv) > 1:
        destination = argv[1]
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        destination = os.path.join(os.path.dirname(source), f"backup-{stamp}.db")
    backup(source, destination)
    print(destination)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
