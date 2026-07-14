# map-service-infra

MAP 서비스의 인프라 오케스트레이션 레포. 다른 4 레포(agent · client · hub · user) 위에 위치하며 컨테이너 구성·환경변수·DB 초기화를 단독 보유한다.

## 역할

- `docker-compose.yml` — PoC 단일 호스트 오케스트레이션 (6 services × 3 profiles)
- `.env.example` 단일 진실원 — 모든 service에 `env_file: ./.env`로 주입
- `db/init/00-create-schemas.sql` — postgres 첫 부팅 시 schema 3개(`user_service`, `hub_data`, `langgraph`) 생성 + grant
- `scripts/map-{up-1-backend,up-2-bff,up-3-client,down}.sh` — PoC 단계별 로컬 기동·정리 (개발/디버그용)

## 폴더 구조

```
map-service-infra/
├── .env.example                  환경변수 템플릿
├── docker-compose.yml            6 services + 3 profiles
├── db/
│   └── init/
│       └── 00-create-schemas.sql 첫 부팅 시 자동 실행
└── scripts/
    ├── map-up-1-backend.sh       Stage1: postgres·redis·hub·agent 기동
    ├── map-up-2-bff.sh           Stage2: user-BFF 로컬 실행(gradlew bootRun)
    ├── map-up-3-client.sh        Stage3: 에뮬레이터 + flutter run
    └── map-down.sh               전체 정리(데이터 볼륨 보존)
```

## profiles

| profile | 포함 service |
|---|---|
| `infra` | postgres · redis |
| `backend` | user · agent · hub · admin |
| `full` | 위 전부 |

## 실행 (macOS · Windows WSL2 · Linux 공통)

```bash
cd map-service-infra
cp .env.example .env                              # API 키 주입
docker compose --profile full up -d --build

# 검증
curl -s http://127.0.0.1:8080/actuator/health     # user
curl -s http://127.0.0.1:8000/health              # agent
curl -s http://127.0.0.1:8001/health              # hub
docker compose exec postgres pg_isready -U map
docker compose exec redis    redis-cli ping
docker compose exec postgres psql -U map -c "\dn" # 3 schemas
```

## PoC 단계별 기동 (로컬 개발·디버그)

`docker compose --profile full up`(전체 컨테이너)과 달리, BFF를 **호스트 JVM(`gradlew bootRun`)**으로
띄우고 agent·hub·postgres·redis만 컨테이너로 두는 **반복 개발용 토폴로지**다.

| 스크립트 | 단계 | 실행 형태 |
|---|---|---|
| `map-up-1-backend.sh` | postgres·redis·hub·agent 기동(`--no-deps`, 시드 확인, 헬스 게이트) | 준비되면 종료 |
| `map-up-2-bff.sh` | user-BFF `gradlew bootRun`(도커 호스트 포트로 연결) | 포그라운드(Ctrl+C 종료) |
| `map-up-3-client.sh` | Android 에뮬레이터 부팅 + `flutter run`(`API_BASE_URL=10.0.2.2:8080`) | 포그라운드(`q` 종료) |
| `map-down.sh` | 앱·BFF·도커 백엔드 정리(데이터 볼륨 보존) | 1회 실행 |

```bash
# 터미널 3개 권장 (Stage 2·3은 포그라운드로 점유)
cd map-service-infra
./scripts/map-up-1-backend.sh     # 터미널 A — 끝나면 프롬프트 복귀
./scripts/map-up-2-bff.sh         # 터미널 B — "Started ServiceUserApplication" 대기
./scripts/map-up-3-client.sh      # 터미널 C — 에뮬레이터에 앱 표시

./scripts/map-down.sh             # 정리(컨테이너·네트워크 제거, 데이터 볼륨 유지)
```

- 추가 사전 준비: `.env`의 유효한 `GEMINI_API_KEY`/`JWT_SECRET`, Android 에뮬레이터(`Pixel_7`), Flutter SDK
- 경로 거리·시간은 agent의 LLM 추정으로 산출한다(도로 라우팅 엔진 미사용). 날씨는 KMA 적재 상태에 따라 빈 배열일 수 있음(정상 동작)
- BFF가 호스트 JVM이라 컨테이너 기동(`--profile full up`)과 토폴로지가 다름에 유의

## 운영 콘솔 + 모니터링 스택 (cut 2)

- **admin(8002)** — 운영 콘솔 JSON API. **admin-web(8003)** — React SPA(nginx,
  `/api` → admin 프록시). 둘 다 `backend`/`full` 프로파일. 브라우저는
  `http://127.0.0.1:8003/` 로 접속(SPA 로그인). 최초 계정은 `.env` 의
  `ADMIN_BOOTSTRAP_USER`/`ADMIN_BOOTSTRAP_PASSWORD` 로 시드된다.
- **모니터링 스택** — `prometheus`(9090)·`grafana`(3000)·`postgres-exporter`·
  `redis-exporter`·`node-exporter`. **별도 `monitoring` 프로파일**(=`full` 미포함,
  Pi 메모리 여유 운용). 전 포트 loopback, 이미지는 multi-arch(arm64 OK).

```bash
# full + monitoring 함께 기동
docker compose --profile full --profile monitoring up -d --build
# Grafana: http://127.0.0.1:3000  (GF_SECURITY_ADMIN_USER/PASSWORD, .env)
# Prometheus: http://127.0.0.1:9090
```

- Prometheus 는 hub/agent/admin `/metrics` 와 user `/actuator/prometheus`,
  exporter 3종을 30s 간격으로 스크레이프한다(보존 10d/2GB — `docker-compose.yml`
  prometheus command 플래그). Grafana 데이터소스·대시보드(서비스 개요·인프라)는
  `monitoring/grafana/provisioning` 으로 코드 프로비저닝된다.
- admin 콘솔의 "모니터링" 화면은 `MONITORING_PANELS`(.env) 로 지정한 Grafana URL 을
  iframe/링크로 임베드한다(Grafana `GF_SECURITY_ALLOW_EMBEDDING=true` + anonymous
  Viewer 전제 — loopback 한정).
- `.env` 추가 키: `GF_SECURITY_ADMIN_USER`, `GF_SECURITY_ADMIN_PASSWORD`.

## 사전 준비

- Docker (Desktop 또는 Engine), `docker compose` v2
- 호스트 RAM 8GB+, 디스크 10GB+ 여유
- 호스트 OS 절전·자동업데이트·화면 잠금 비활성화 권장

## License

MIT — see [LICENSE](LICENSE).
