# Python Docker Reverse Proxy

aiohttp로 직접 구현한 경량 HTTP reverse proxy입니다. Docker Compose로 실행되는 웹 서비스와 MCP 서버를 **도메인(Host 헤더)별로** 라우팅합니다. Nginx, Traefik, Caddy 같은 기존 프록시는 쓰지 않습니다.

- 서버 **IP 주소**로 접속하면 → 관리자 페이지 (로그인 필요)
- 등록된 **도메인**으로 접속하면 → 해당 Docker 서비스로 프록시
- 관리자 페이지에서 바꾼 라우팅은 재시작 없이 바로 반영되고, SQLite에 영구 저장됩니다.

```text
                         ┌──────────────── reverse-proxy 컨테이너 (:80) ────────────────┐
 http://192.168.0.10 ──▶ │ Host = IP     → 관리자 UI (IP 허용 목록 + 로그인 + CSRF)        │
                         │                                                               │
 http://test.com/... ──▶ │ Host = 도메인 → 라우팅 캐시 조회 → http://test-app:1000/...      │──▶ test-app
 http://mcp.test.com ──▶ │                (스트리밍 중계, SSE/MCP 포함) → mcp-server:3000  │──▶ mcp-server
                         └──────────────────────────┬────────────────────────────────────┘
                                                    │ Docker 내장 DNS (서비스명 → 컨테이너 IP)
                                     proxy-network (공통 Docker 네트워크)
```

## 실행 요구사항

- Docker Engine 20.10 이상, Docker Compose v2
- 호스트의 80번 포트 (`PROXY_HTTP_PORT`로 바꿀 수 있음)
- 로컬에서 테스트를 실행하려면 Python 3.12 이상

## 빠른 시작

```bash
cp .env.example .env
# .env 에서 ADMIN_PASSWORD(8자 이상)와 SESSION_SECRET(32자 이상)을 반드시 설정
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # SESSION_SECRET 생성 예

docker compose up -d --build
docker compose ps        # reverse-proxy, test-app, mcp-server 가 healthy 인지 확인
```

`ADMIN_PASSWORD`나 `SESSION_SECRET`이 비어 있으면 `docker compose`가 바로 오류를 내고 멈춥니다. 비밀번호와 서명 키는 소스코드에 기본값이 없습니다.

`compose.yml`에는 테스트용 백엔드 `test-app`(일반 HTTP API, 포트 1000)과 `mcp-server`(MCP 테스트 서버, 포트 3000)가 함께 들어 있습니다. 둘 다 호스트에 포트를 공개하지 않고 프록시를 거쳐서만 접근합니다. 운영 환경에서는 이 두 서비스를 지워도 됩니다.

## 관리자 계정과 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | 관리자 아이디 |
| `ADMIN_PASSWORD` | (필수) | 관리자 비밀번호, 8자 이상 |
| `SESSION_SECRET` | (필수) | 세션 쿠키 HMAC 서명 키, 32자 이상 |
| `PROXY_HTTP_PORT` | `80` | 호스트 공개 포트 (컨테이너 내부는 항상 80) |
| `PROXY_NETWORK_EXTERNAL` | `false` | `proxy-network`를 미리 만들어 관리할 때 `true` |
| `ADMIN_ALLOWED_IPS` | 루프백 + 사설망 | 관리자 페이지 접근 허용 IP/CIDR (쉼표 구분, `*`은 전체) |
| `SESSION_TTL_SECONDS` | `28800` | 관리자 세션 유효 시간 |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_SECONDS` | `5` / `300` | IP별 로그인 실패 허용 횟수와 잠금 시간 |
| `TRUSTED_PROXIES` | (없음) | X-Forwarded-*를 신뢰할 앞단 프록시 IP/CIDR |
| `ALLOWED_TARGET_SERVICES` | (없음 = 형식만 검사) | 라우팅 대상으로 허용할 서비스명 목록 |
| `BLOCKED_TARGET_SERVICES` | `localhost,reverse-proxy` | 라우팅 대상으로 금지할 서비스명 |
| `ALLOWED_TARGET_NETWORKS` | 사설망 | 서비스명이 해석된 IP가 속해야 하는 대역 |
| `PROXY_CONNECT_TIMEOUT` | `5` | 백엔드 연결 타임아웃(초), 초과 시 504 |
| `PROXY_READ_TIMEOUT` | `60` | 응답 헤더 대기 타임아웃(초), 초과 시 504. 본문 스트리밍에는 적용 안 됨, `0`이면 끔 |
| `PROXY_KEEPALIVE_TIMEOUT` | `4` | 백엔드 keep-alive 연결 유지 시간(초) |
| `PROXY_POOL_LIMIT` | `1000` | 동시 업스트림 연결 수 상한 (SSE 장기 연결 포함) |
| `LOG_LEVEL` | `INFO` | 로그 레벨 |

비밀번호에 `$` 같은 특수문자가 있으면 `.env`에서 작은따옴표로 감싸세요: `ADMIN_PASSWORD='pa$$word'`.

## 관리자 페이지 접속

브라우저 주소창에 서버 IP를 입력합니다.

```text
http://192.168.0.10          # PROXY_HTTP_PORT=80
http://192.168.0.10:8080     # PROXY_HTTP_PORT=8080
http://127.0.0.1             # 서버 자신에서 접속
```

- Host 헤더가 IP 주소인지는 `ipaddress` 표준 라이브러리로 판별합니다 (IPv4와 `[IPv6]` 모두).
- IP로 접속했다는 사실만으로 권한이 생기지는 않습니다. 접속 IP가 `ADMIN_ALLOWED_IPS`에 들어 있어야 하고(아니면 403), 로그인해야 합니다.
- 기본 허용 범위는 루프백과 사설망(10/8, 172.16/12, 192.168/16, fc00::/7)입니다. 공인 IP에서 관리해야 한다면 `ADMIN_ALLOWED_IPS`에 해당 IP만 추가하세요.
- 도메인으로 접속한 요청은 경로와 상관없이(`/login` 포함) 관리자 페이지에 닿지 않습니다. 등록된 도메인이면 백엔드로 전달되고, 아니면 404가 납니다.

## 라우팅 등록

1. 관리자 페이지에 로그인합니다.
2. **라우팅 추가**에 입력합니다.
   - 도메인: `test.com` (정확히 일치하는 도메인만 처리하고, 대소문자는 구분하지 않으며, 와일드카드는 지원하지 않음)
   - Docker 서비스명: `test-app` (IP 주소, URL, 점이 들어간 외부 도메인은 거부)
   - 내부 포트: `1000` (호스트에 공개한 포트가 아니라 **컨테이너 내부 포트**)
   - 활성화: 체크를 풀면 404로 응답 (설정은 남아 있음)
   - 원래 Host 헤더 전달: 기본은 켜짐. 백엔드가 `Host: 서비스명:포트`를 요구할 때만 끄세요.
3. 저장하면 곧바로 `http://test.com/...` 요청이 `http://test-app:1000/...`으로 전달됩니다.

목록에서 수정, 활성/비활성 전환, 삭제를 할 수 있고, 모두 즉시 반영됩니다. 같은 도메인을 두 번 등록할 수 없습니다(대소문자, 끝의 `.` 무시).

| 요청 | 전달 대상 |
|---|---|
| `http://test.com/users` | `http://test-app:1000/users` |
| `http://api.test.com/v1/items` | `http://api-server:8080/v1/items` |
| `http://mcp.test.com/mcp` | `http://mcp-server:3000/mcp` |

> 클라이언트가 도메인으로 접속하려면 해당 도메인이 이 서버 IP를 가리켜야 합니다 (DNS A 레코드, 또는 테스트용으로 `/etc/hosts`).

## Docker 네트워크와 다른 Compose 프로젝트 연결

프록시와 백엔드는 공통 네트워크 `proxy-network`에서 통신합니다. 프록시는 Docker 내장 DNS로 **서비스명을 매 연결마다 다시 해석**합니다. 그래서 컨테이너가 재생성되어 IP가 바뀌어도 라우팅 설정을 고칠 필요가 없습니다.

### 방법 A: 프록시 프로젝트가 네트워크를 만듦 (기본)

`docker compose up`을 실행하면 `proxy-network`가 자동으로 생깁니다. 다른 프로젝트는 이 네트워크를 `external`로 참조합니다. 이때는 프록시를 먼저 띄워야 합니다.

### 방법 B: 네트워크를 미리 직접 만듦 (여러 프로젝트를 운영할 때 권장)

```bash
docker network create proxy-network
echo "PROXY_NETWORK_EXTERNAL=true" >> .env
docker compose up -d --build
```

네트워크의 생명주기가 프록시 프로젝트와 분리됩니다. 백엔드 프로젝트를 먼저 띄워도 되고, 프록시 쪽에서 `docker compose down`을 해도 네트워크가 지워지지 않습니다.

### 다른 프로젝트의 compose.yml

```yaml
services:
  api-server:
    image: my-api:latest
    # ports: 는 필요 없음 (프록시가 내부 포트로 직접 연결)
    networks:
      - proxy-network

networks:
  proxy-network:
    external: true
    name: proxy-network
```

예제: [`examples/external-project/compose.yml`](examples/external-project/compose.yml)

```bash
docker compose -f examples/external-project/compose.yml up -d --build
# 관리자 페이지에서 api.test.com → api-server : 8080 등록
curl -H "Host: api.test.com" http://192.168.0.10/v1/items
```

이미 실행 중인 컨테이너는 `docker network connect proxy-network <컨테이너명>`으로 연결할 수도 있습니다. 여러 프로젝트에 같은 서비스명이 있으면 DNS가 둘 다 돌려주므로, 공통 네트워크에 붙는 서비스명은 서로 겹치지 않게 지으세요.

## 일반 HTTP 서버 연결 예시

```yaml
# 다른 프로젝트의 compose.yml
services:
  blog:
    image: ghost:5
    environment:
      url: http://blog.example.com
    networks: [proxy-network]
networks:
  proxy-network:
    external: true
    name: proxy-network
```

관리자 페이지에서 `blog.example.com` → `blog` : `2368`을 등록하면 됩니다.

## MCP 서버 연결 예시

프록시는 MCP 서버를 구현하지 않습니다. MCP 클라이언트와 서버 사이의 HTTP를 그대로 중계합니다. JSON-RPC 본문을 해석하거나 버전을 변환하지 않으며 상태 코드와 `Content-Type`도 바꾸지 않습니다.

- **Streamable HTTP**: `POST /mcp` (JSON 또는 `text/event-stream` 응답), `GET /mcp` (서버→클라이언트 SSE), `DELETE /mcp`
- **HTTP+SSE (2024-11-05 구버전)**: `GET /sse` + `POST /messages?session_id=...`
- `MCP-Protocol-Version`, `Mcp-Session-Id`, `Mcp-Method`, `Mcp-Name`, `Mcp-Param-*`, `Last-Event-ID`, `Accept`, `Authorization`, `Origin` 헤더는 그대로 전달됩니다. 세션은 프록시가 관리하지 않습니다.
- SSE 이벤트는 받는 즉시 클라이언트로 흘려보냅니다. 응답 본문에는 타임아웃이 없어 오래 열려 있는 스트림도 유지되고, 클라이언트가 끊으면 백엔드 연결도 바로 닫힙니다.

관리자 페이지에서 `mcp.test.com` → `mcp-server` : `3000`을 등록한 뒤, MCP 클라이언트에 다음 URL을 설정합니다.

```jsonc
// 예: MCP 클라이언트 설정
{
  "mcpServers": {
    "test": { "type": "http", "url": "http://mcp.test.com/mcp" }
  }
}
```

```bash
# Claude Code 예시
claude mcp add --transport http test-mcp http://mcp.test.com/mcp
```

예제 MCP 서버(`examples/mcp-server`)가 제공하는 도구는 `echo`, `add`, `countdown`(진행 알림을 SSE로 스트리밍), `inspect_headers`(서버가 받은 MCP 헤더 확인)입니다.

## curl 테스트 예시

DNS를 설정하지 않았다면 `-H "Host: ..."`나 `--resolve`로 도메인을 지정합니다. `SERVER=192.168.0.10`이라고 가정합니다.

```bash
# 일반 HTTP (test.com → test-app:1000 등록 후)
curl -H "Host: test.com" "http://$SERVER/users?page=2"
curl --resolve test.com:80:$SERVER http://test.com/echo/hello
curl -H "Host: test.com" -H "Content-Type: application/json" -d '{"name":"kim"}' http://$SERVER/echo/json

# 큰 파일 업/다운로드 (스트리밍)
curl -H "Host: test.com" "http://$SERVER/download?size=1073741824&chunked=1" -o /dev/null
head -c 1073741824 /dev/zero | curl -X POST -T - -H "Host: test.com" http://$SERVER/upload

# SSE: 0.5초마다 이벤트가 하나씩 바로 도착하는지 확인
curl -N -H "Host: test.com" "http://$SERVER/sse?count=5&interval=0.5"

# MCP Streamable HTTP (mcp.test.com → mcp-server:3000 등록 후)
curl -i -H "Host: mcp.test.com" -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}' \
     http://$SERVER/mcp
# 응답 헤더의 Mcp-Session-Id 값을 SID 로 사용
curl -N -H "Host: mcp.test.com" -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -H "Mcp-Session-Id: $SID" -H "MCP-Protocol-Version: 2025-06-18" \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"countdown","arguments":{"count":5,"interval":1},"_meta":{"progressToken":"t1"}}}' \
     http://$SERVER/mcp

# 오류 처리
curl -i -H "Host: unknown.example" http://$SERVER/         # 404 등록되지 않은 도메인
docker compose stop test-app
curl -i -H "Host: test.com" http://$SERVER/                # 502 백엔드 연결 실패
docker compose start test-app                              # 재시작 후 자동 복구
```

## 로그 확인

```bash
docker compose logs -f reverse-proxy          # 프록시 + 관리자 로그
docker compose logs -f --tail=100             # 전체 서비스
```

접근 로그 형식은 `클라이언트IP "Host" "요청줄" 상태 바이트 처리시간 "User-Agent"`입니다. 백엔드 연결 실패나 타임아웃, 로그인 실패, 라우팅 변경은 각각 `WARNING`/`INFO`로 남습니다. 컨테이너 HEALTHCHECK 요청은 접근 로그에서 뺐습니다. 로그 파일은 컨테이너당 최대 10MB × 5개로 로테이션됩니다.

## SQLite 백업과 복원

라우팅 설정은 Docker Volume `reverse-proxy-data`의 `/app/data/proxy.db`에 저장됩니다. `docker compose down`이나 컨테이너 재생성 후에도 유지됩니다 (`down -v`는 Volume까지 지우니 주의하세요).

```bash
# 실행 중에도 일관된 스냅샷을 만드는 SQLite backup API 사용
docker compose exec reverse-proxy python -m app.database.backup /app/data/backup.db
docker compose cp reverse-proxy:/app/data/backup.db ./proxy-backup-$(date +%Y%m%d).db

# 복원: 프록시를 멈추고 파일을 교체한 뒤 다시 시작
docker compose stop reverse-proxy
docker run --rm -v reverse-proxy-data:/data -v "$PWD":/backup alpine \
  sh -c 'rm -f /data/proxy.db-wal /data/proxy.db-shm && cp /backup/proxy-backup-YYYYMMDD.db /data/proxy.db && chown 10001:10001 /data/proxy.db'
docker compose start reverse-proxy
```

## 테스트

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

테스트는 Docker 없이 한 프로세스 안에서 프록시와 예제 백엔드를 띄워 검증합니다. 라우팅, 경로와 Query 유지, 메서드, JSON, 32~48MB 본문 스트리밍, SSE 실시간 전달, MCP(Streamable HTTP와 구버전 SSE, 장시간 연결, 헤더 전달), 관리자 로그인/CSRF/시도 제한, 라우팅 CRUD 즉시 반영, SQLite 영구 저장, 404/502/504, 백엔드 재시작 후 복구, IP와 도메인 접근 분기를 다룹니다. 공식 MCP Python SDK(`pip install "mcp>=2"`)가 설치되어 있으면 SDK 클라이언트로 하는 검증(`tests/test_mcp_sdk.py`)도 함께 실행됩니다.

## 동작 상세

**요청 처리 순서**: Host 헤더 정규화(포트 분리, 소문자, 끝의 `.` 제거) → IP면 관리자 → 도메인이면 메모리 라우팅 캐시에서 정확히 일치하는 항목 조회 → `http://서비스명:포트` + 원래 경로·Query(인코딩 그대로)로 전송 → 응답 스트리밍.

**스트리밍**: 요청과 응답 본문을 메모리에 모으지 않고 받은 만큼 바로 전달합니다. 2GiB를 업·다운로드하는 동안에도 프록시 메모리는 약 35MB로 유지됐습니다. Chunked 요청/응답을 지원하고, Content-Encoding이 있는 본문은 풀지 않고 그대로 보냅니다. 그래서 Content-Length와 Content-Encoding이 어긋나지 않습니다.

**연결**: 클라이언트 쪽은 HTTP/1.1 keep-alive를 지원합니다. 백엔드 쪽은 커넥션 풀을 재사용하고 DNS는 캐시하지 않습니다. 재사용하던 연결이 막 닫힌 경우, 본문이 없는 GET/HEAD/OPTIONS에 한해 한 번 다시 시도합니다.

**헤더**:
- Hop-by-hop 헤더(`Connection`, `Keep-Alive`, `Transfer-Encoding`, `TE`, `Upgrade`, `Proxy-*`)와 `Connection`에 나열된 헤더는 제거합니다.
- 나머지 헤더(`Authorization`, `Cookie`, `Accept`, `Content-Type`, MCP 헤더 등)는 순서와 중복을 유지해 전달합니다. 여러 개의 `Set-Cookie`도 그대로 전달합니다.
- `X-Forwarded-For/Host/Proto`, `X-Real-IP`, `Forwarded`는 직접 연결한 peer가 `TRUSTED_PROXIES`에 속할 때만 이어 붙이고, 그 밖에는 버린 뒤 새로 만듭니다. 그래서 클라이언트가 위조한 값은 전달되지 않습니다.
- `Via` 헤더를 붙여 라우팅 루프(자기 자신을 가리키는 설정)를 감지하면 508을 돌려줍니다.

**타임아웃**:
- 연결: `PROXY_CONNECT_TIMEOUT`
- 응답 헤더 대기: `PROXY_READ_TIMEOUT`. 업로드 중에는 청크가 올 때마다 연장되는 유휴 타임아웃입니다.
- 응답 본문(SSE 포함): 타임아웃 없음

**오류 응답**

| 상황 | 상태 |
|---|---|
| 등록되지 않았거나 비활성화된 도메인 | 404 |
| 백엔드 연결 실패, DNS 실패, 허용되지 않은 대상 IP, 잘못된 백엔드 응답 | 502 |
| 백엔드 연결/응답 타임아웃 | 504 |
| 관리자 미인증 | 로그인 화면으로 이동 (POST는 401), 로그인 실패 401, 시도 초과 429 |
| 잘못된 라우팅 입력, 도메인 중복 | 400 |
| Host 헤더가 없거나 잘못됨, absolute-form 요청 | 400 |
| 지원하지 않는 메서드 (TRACE, CONNECT 등) | 405 |
| 응답 본문 전송 중 백엔드가 끊김 | 클라이언트 연결을 끊어 응답이 잘렸음을 알림 |

백엔드 하나가 멈춰도 프록시는 계속 동작하고 다른 도메인은 정상 처리됩니다.

## 프로젝트 구조

```text
├── app/
│   ├── main.py              # 진입점, Host 기반 분기(관리자/프록시), 앱 수명주기
│   ├── config.py            # 환경변수 설정 및 검증
│   ├── net.py               # Host 파싱, IP 판별, 신뢰 프록시 기반 클라이언트 IP
│   ├── validation.py        # 도메인/서비스명/포트 검증, 대상 허용 정책
│   ├── service.py           # 라우팅 변경 = DB 커밋 + 캐시 교체 (일관성 보장)
│   ├── context.py           # aiohttp AppKey
│   ├── healthcheck.py       # Docker HEALTHCHECK
│   ├── proxy/
│   │   ├── handler.py       # 스트리밍 프록시, 타임아웃, 오류 처리
│   │   ├── router.py        # 메모리 라우팅 테이블
│   │   ├── headers.py       # hop-by-hop / X-Forwarded-* 처리
│   │   └── resolver.py      # SSRF 방지 DNS resolver
│   ├── admin/
│   │   ├── handlers.py      # 관리자 화면 (SSR)
│   │   └── auth.py          # 세션, CSRF, 로그인 시도 제한
│   ├── database/
│   │   ├── models.py        # 스키마, 모델
│   │   ├── repository.py    # aiosqlite 저장소
│   │   └── backup.py        # 온라인 백업 CLI
│   ├── templates/           # Jinja2 (login, dashboard, edit)
│   └── static/              # CSS / JS (빌드 도구 없음)
├── examples/
│   ├── test-app/            # 일반 HTTP API 테스트 백엔드 (:1000)
│   ├── mcp-server/          # MCP 테스트 서버 (:3000, Streamable HTTP + HTTP+SSE)
│   └── external-project/    # 별도 Compose 프로젝트 연결 예시 (api-server:8080)
├── tests/                   # pytest
├── Dockerfile
├── compose.yml
├── requirements.txt / requirements-dev.txt
└── .env.example
```

## 지원하지 않는 기능

- TLS/HTTPS 종료 (HTTP만 지원)
- WebSocket (`Upgrade` 헤더는 제거됨), MCP stdio 전송
- 와일드카드/서브도메인 자동 매칭, 경로 기반 라우팅, 로드밸런싱
- IP 주소로 들어온 요청의 프록시 (IP 접속은 항상 관리자 페이지)
- HTTP/2, CONNECT 같은 포워드 프록시 기능, TRACE/WebDAV 메서드
- 여러 프록시 인스턴스 동시 실행 (라우팅 캐시와 관리자 세션이 프로세스 메모리에 있음)

## 보안 주의사항

이 프록시는 같은 Docker 네트워크의 다른 서비스에 접근할 수 있으므로 다음을 꼭 확인하세요.

- **인터넷에 공개한다면 HTTPS가 별도로 필요합니다.** 이 버전은 평문 HTTP만 처리하므로 관리자 비밀번호, 세션 쿠키, `Authorization` 헤더, MCP 토큰이 암호화되지 않습니다. 앞단에 TLS를 종료하는 장치(클라우드 LB 등)를 두고 그 IP를 `TRUSTED_PROXIES`에 넣거나, VPN/사설망 안에서만 사용하세요.
- **관리자 페이지 노출**: 서버 IP로 접속할 수 있는 사람은 누구나 로그인 화면에 닿을 수 있습니다. `ADMIN_ALLOWED_IPS`를 관리자 PC나 VPN 대역으로 좁히고, 방화벽이나 보안 그룹으로도 막으세요. 강한 비밀번호와 충분히 긴 `SESSION_SECRET`을 쓰고, `.env`는 버전 관리에 넣지 마세요(`.gitignore`에 포함됨).
- **라우팅 대상 제한 (SSRF / 오픈 프록시 방지)**
  - 대상은 단일 레이블 Docker 서비스명과 포트만 받습니다. IP, URL, 외부 도메인, `localhost`, 프록시 자신은 입력 단계에서 거부합니다.
  - 서비스명이 해석된 IP도 `ALLOWED_TARGET_NETWORKS`(기본 사설망) 안에 있어야 연결합니다. 공인 IP, 루프백, 링크로컬(클라우드 메타데이터 169.254.169.254)로는 연결하지 않습니다.
  - 운영 환경에서는 `ALLOWED_TARGET_SERVICES`로 허용할 서비스를 명시하는 것을 권장합니다.
  - absolute-form 요청(`GET http://other/ HTTP/1.1`)과 CONNECT/TRACE는 거부하고, 리다이렉트를 따라가지 않으며, 백엔드 쿠키를 저장하지 않습니다.
- **관리자 인증**
  - 세션 쿠키는 `HttpOnly`, `SameSite=Strict`이고, 쿠키에는 HMAC으로 서명한 세션 ID만 담습니다.
  - 상태를 바꾸는 모든 요청은 POST + CSRF 토큰 + Origin 검사를 거칩니다.
  - 로그인 실패는 IP별로 제한합니다. 관리자 화면에는 비밀번호나 서명 키를 표시하지 않습니다.
  - 프록시가 재시작되면 관리자 세션이 초기화됩니다.
- **클라이언트 IP**: Docker 설정(userland-proxy 등)에 따라 컨테이너가 보는 접속 IP가 Docker 게이트웨이 주소(예: `172.18.0.1`)일 수 있습니다. 이 경우 관리자 IP 허용 목록과 로그인 시도 제한이 그 주소를 기준으로 동작하니 환경에 맞게 확인하세요.
- **컨테이너**: 프록시 컨테이너는 비 root 사용자, 읽기 전용 루트 파일시스템, `cap_drop: ALL`, `no-new-privileges`로 실행됩니다.
