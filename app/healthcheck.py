"""Docker HEALTHCHECK 용 스크립트: ``python -m app.healthcheck``.

컨테이너 내부에서 127.0.0.1 로 접속하므로 IP 접속 → 관리자 앱의 /healthz 로 분기된다.
"""

from __future__ import annotations

import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("LISTEN_PORT") or "80"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except Exception as exc:  # noqa: BLE001
        print(f"health check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
