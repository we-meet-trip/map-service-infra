# map-service-infra

MAP 서비스의 인프라 오케스트레이션 레포. 다른 6 레포(admin · agent · client · hub · user · yolo) 위에 위치하며 컨테이너 구성·환경변수·DB 초기화를 단독 보유한다.

## 두 개의 스택

컨테이너를 역할로 갈라 두 스택으로 운영한다. 평소 개발에는 서비스 스택만 띄우고, 운영 화면이 필요할 때 관리자 스택을 추가로 올린다.

| 스택 | 파일 | 프로젝트명 | 서비스 |
|---|---|---|---|
| 서비스 | `docker-compose.yml` | `map-service` | postgres · redis · user · agent · hub · yolo · proxy · osrm-foot · osrm-bicycle |
| 관리자 | `docker-compose.admin.yml` | `map-admin` | admin · admin-web · prometheus · grafana · exporter 3종 |

- 컨테이너 이름은 `map-service-hub`, `map-admin-prometheus` 처럼 스택과 역할이 드러나게 고정한다(운영 콘솔 API 만 `map-admin-api`).
- 네트워크 `map-net` 은 **서비스 스택이 소유**한다. 그래서 **기동은 서비스 → 관리자**, **종료는 관리자 → 서비스** 순서여야 한다.
- 데이터 볼륨 이름은 `map_postgres-data` 처럼 고정해 두었다. 프로젝트명을 바꾸기 전에 쌓인 데이터를 그대로 이어 쓰기 위해서다. 단 `osrm-data` 만 접두 없이 그 이름 그대로이며, `scripts/osrm-rebuild.sh` 의 `OSRM_VOLUME` 기본값과 짝을 이룬다.
- 이름을 고정한 대가로 **`docker compose down -v` 가 볼륨을 지우지 못한다.** 볼륨 라벨은 옛 프로젝트명 `map` 이고 현재 프로젝트명은 `map-service` 라 대상으로 잡히지 않는다(osrm-data 는 라벨 자체가 없다). 지울 때는 `docker volume rm <이름>` 으로 명시한다.

## 역할

- `docker-compose.yml` — 서비스 스택 (9 services × 5 profiles)
- `docker-compose.admin.yml` — 관리자 스택 (7 services, `monitoring` 프로파일 분리)
- `.env.example` 단일 진실원 — 두 스택 모두 `env_file: ./.env` 로 주입
- `db/init/00-create-schemas.sql` — postgres 첫 부팅 시 schema 4개(`user_service`, `hub_data`, `langgraph`, `admin_data`) 생성 + PostGIS 확장 + `search_path` 설정
- `db/init/10-admin.sh` — 운영 콘솔용 `map_admin` 역할 생성과 권한(GRANT) 부여. 파일명 순서(00 → 10)대로 실행되어야 한다 — 앞 파일이 만든 스키마에 권한을 걸기 때문이다
- `scripts/map-{up-1-backend,up-2-bff,up-3-client,down}.sh` — 단계별 로컬 기동·정리 (개발/디버그용)
- `scripts/map-up-admin.sh` — 관리자 스택 기동(DB 준비 확인 후)
- `scripts/map-serve{,-down}.sh` — 실기기용 외부 노출(터널 개통 + 앱이 읽는 주소 게시)
- `proxy/default.conf` — 외부 노출 시 앞에 서는 관문(nginx) 설정
- `docker-compose.admin.registry.yml` — 관리자 스택을 만들지 않고 받아 쓴다
- `docker-compose.admin.test.yml` — 관리자 스택을 시험 스택 곁에 세운다(포트·볼륨·네트워크 분리)
- `scripts/cloud-up.sh` — 클라우드 서버에서 순서대로 띄운다(받기 → 저장소 → 백업 → 표 손질 → 앱 → 콘솔).
  `--test` 시험 스택 · `--micro` 1GB 급 서버 · `--registry` 만들지 않고 받아 쓰기 ·
  `--edge` 바깥 노출 · `--routing` 경로 엔진 · `--vision` 카메라 인식 ·
  `--admin` 운영 콘솔 · `--monitoring` 콘솔 + 지표
- `scripts/images-push.sh` — 여섯 이미지를 만들어 받아갈 곳에 올린다. 평소에는 사람이
  직접 부르지 않고 레포의 배포 실행이 부른다
- `scripts/make-test-env.sh` — 본보기에서만 파생해 시험용 환경파일을 만든다.
  실제 발급처 키가 시험으로 넘어갈 길을 구조적으로 막는다
- `scripts/compose-isolation-check.sh` — 운영과 시험이 정말 갈라져 있는지 본다.
  만들어 쓰는 조합과 받아 쓰는 조합, 콘솔 스택, 그리고 서비스와 콘솔 사이까지
  다섯 조합을 렌더링한다(서비스↔콘솔은 네트워크를 일부러 공유하므로 그 축만 뺀다)
- `scripts/sync-client-env.sh` — 앱이 읽는 환경파일을 손으로 적지 않고 만든다
- `scripts/e2e_full.py` · `scripts/e2e_chat.py` · `scripts/e2e_chat_multidevice.py` —
  띄운 스택을 실제로 두드려 보는 검증. 사람이 손으로 돌린다
- `scripts/log_audit.py` — 로그와 지표를 빠짐없이 훑는다. 위 셋이 요청·저장·통신을
  본다면 이것은 부작용과 침묵을 본다

## 폴더 구조

```
map-service-infra/
├── .env.example                  환경변수 템플릿
├── docker-compose.yml            서비스 스택 (9 services + 5 profiles)
├── docker-compose.admin.yml      관리자 스택 (7 services)
├── docker-compose.admin.registry.yml  관리자 스택 받아 쓰기
├── docker-compose.admin.test.yml      관리자 스택 시험 덧칠
├── db/
│   └── init/
│       ├── 00-create-schemas.sql 첫 부팅 시 자동 실행 (schema 4개 + PostGIS + search_path)
│       └── 10-admin.sh           map_admin 역할·권한 (00 다음에 실행)
├── monitoring/
│   ├── prometheus/prometheus.yml 스크레이프 대상
│   └── grafana/                  데이터소스·대시보드 프로비저닝
├── proxy/
│   └── default.conf              외부 노출 관문(nginx) 설정
└── scripts/
    ├── map-up-1-backend.sh       Stage1: postgres·redis·hub·agent 기동
    ├── map-up-2-bff.sh           Stage2: user-BFF 로컬 실행(gradlew bootRun)
    ├── map-up-3-client.sh        Stage3: 에뮬레이터 + flutter run
    ├── map-up-admin.sh           관리자 스택 기동(콘솔 / --monitoring)
    ├── map-serve.sh              실기기용 외부 노출(터널 + 주소 게시)
    ├── map-serve-down.sh         외부 노출만 종료(스택 유지)
    ├── map-down.sh               전체 정리(데이터 볼륨 보존)
    ├── osrm-rebuild.sh           라우팅 그래프 빌드
    └── pg-backup.sh              DB 덤프 백업
```

## profiles (서비스 스택)

| profile | 포함 service | 비고 |
|---|---|---|
| `infra` | postgres · redis | |
| `backend` | user · agent · hub · proxy | |
| `full` | 위 전부 | |
| `vision` | yolo | `full` 미포함. 카메라 인식을 쓸 때만 |
| `routing` | osrm-foot · osrm-bicycle | `full` 미포함. `map-serve.sh` 가 함께 올린다(실패해도 나머지는 열린다) |

관리자 스택은 admin·admin-web 이 프로파일 없이 항상 뜨고, 모니터링 5종만 `monitoring` 프로파일이다.

## 실행

compose 명령은 macOS · Windows WSL2 · Linux 어디서나 같다. 다만 아래 `scripts/` 는 macOS 전제다(adb 경로·`open -a Docker`·`caffeinate`).

```bash
cd map-service-infra
cp .env.example .env                              # API 키 주입

# 서비스 스택 — hub 스키마는 자동 생성되지 않으므로 인프라 → 마이그레이션 → 앱 순서로 띄운다
docker compose --profile infra up -d
docker compose run --rm --no-deps --entrypoint alembic hub upgrade head
docker compose --profile full up -d --build
docker compose --profile full --profile vision up -d --build   # 카메라 인식까지
docker compose --profile routing up -d osrm-foot osrm-bicycle   # 도로 추종 경로까지

# 관리자 스택 (서비스 스택이 뜬 뒤에)
./scripts/map-up-admin.sh                # 콘솔만
./scripts/map-up-admin.sh --monitoring   # 콘솔 + 지표

# 검증
curl -s http://127.0.0.1:8080/actuator/health     # user
curl -s http://127.0.0.1:8000/health              # agent
curl -s http://127.0.0.1:8001/health              # hub
curl -s http://127.0.0.1:8004/health              # yolo (vision 프로파일)
curl -s http://127.0.0.1:8002/health              # admin (관리자 스택)
curl -s http://127.0.0.1:8090/healthz             # 관문
curl -s "http://127.0.0.1:5000/nearest/v1/foot/126.9780,37.5665"     # 경로 엔진 도보
curl -s "http://127.0.0.1:5001/nearest/v1/bicycle/126.9780,37.5665"  # 경로 엔진 자전거
docker compose exec postgres pg_isready -U map
docker compose exec redis    redis-cli ping
docker compose exec postgres psql -U map -c "\dn" # 도메인 스키마 4개 + public = 5행
```

## 카메라 인식 (vision)

폰 카메라로 대상을 비추고 물어보면 무엇인지 답해 주는 기능이다. 클라이언트가 관문의 `/ws/vision` 으로 WebSocket 을 맺고, 관문이 이를 `yolo` 로 넘긴다.

```bash
docker compose --profile vision up -d --build yolo
docker compose restart proxy      # 관문 설정을 바꾼 뒤에만 필요
```

- 이미지에 추론 라이브러리가 들어가 무겁다(최초 빌드만 오래 걸리고 이후는 캐시). 그래서 `full` 이 아니라 `vision` 프로파일로 갈라 두었다.
- 모델 가중치는 이미지 빌드 시 내장되므로 기동에 네트워크가 필요 없다.
- 이 서비스는 자체 인증이 없다. 관문에서 접속 시도 횟수와 동시 연결 수만 제한한다.
- `yolo` 가 떠 있지 않아도 관문은 정상 기동하며, 그 경로만 502 를 준다.
- 호출 한도를 agent 와 나누고 싶지 않다면 `.env` 의 `VISION_GEMINI_API_KEY` 에 별도 키를 채운다(비우면 공용 키를 쓴다).

## 도로 추종 경로 (routing)

화면의 이동 경로를 실제 도로 모양으로 그리는 기능이다. BFF 가 hub 의 `/v1/directions/batch` 를 부르고, hub 가 도보·자전거 두 엔진에 물어본다.

```bash
./scripts/osrm-rebuild.sh                          # 그래프 빌드(최초 1회, 오래 걸린다)
docker compose --profile routing up -d osrm-foot osrm-bicycle
```

- 엔진은 미리 만들어 둔 그래프 파일을 읽어야 뜬다. 그래서 `full` 이 아니라 `routing` 프로파일로 갈라 두었다 — 그래프가 없는 환경에서 스택 전체 기동이 막히지 않게 하기 위해서다.
- `.env` 의 `OSRM_FOOT_BASE_URL` · `OSRM_BICYCLE_BASE_URL` 이 채워져 있어야 hub 가 엔진을 부른다. 비어 있으면 엔진이 떠 있어도 hub 자체 대체 경로를 쓴다.
- 엔진이 없으면 hub 가 해당 구간을 비워 응답하고, BFF 는 그 구간을 두 점 잇는 직선으로 접는다. 앱은 그 상태로도 끝까지 동작하므로 겉으로는 정상처럼 보인다.
- hub 는 성공한 경로만 이레 동안 캐시에 담는다. 그래서 엔진이 빠진 뒤에도 이미 담긴 구간은 계속 도로를 따라가고, 새 구간만 직선이 된다.
- `map-serve.sh` 는 이 프로파일을 함께 올리고 준비 여부를 알려 준다. 기동에 실패해도 나머지 노출 절차는 그대로 진행된다.

## 단계별 기동 (로컬 개발·디버그)

`docker compose --profile full up`(전체 컨테이너)과 달리, BFF를 **호스트 JVM(`gradlew bootRun`)**으로
띄우고 agent·hub·postgres·redis만 컨테이너로 두는 **반복 개발용 토폴로지**다.

| 스크립트 | 단계 | 실행 형태 |
|---|---|---|
| `map-up-1-backend.sh` | postgres·redis·hub·agent 기동(`--no-deps`, 시드 확인, 헬스 게이트) | 준비되면 종료 |
| `map-up-2-bff.sh` | user-BFF `gradlew bootRun`(도커 호스트 포트로 연결) | 포그라운드(Ctrl+C 종료) |
| `map-up-3-client.sh` | Android 에뮬레이터 부팅 + `flutter run`(`API_BASE_URL=10.0.2.2:8080`) | 포그라운드(`q` 종료) |
| `map-down.sh` | 앱·BFF·두 스택 정리(데이터 볼륨 보존) | 1회 실행 |

```bash
# 터미널 3개 권장 (Stage 2·3은 포그라운드로 점유)
cd map-service-infra
./scripts/map-up-1-backend.sh     # 터미널 A — 끝나면 프롬프트 복귀
./scripts/map-up-2-bff.sh         # 터미널 B — "Started ServiceUserApplication" 대기
./scripts/map-up-3-client.sh      # 터미널 C — 에뮬레이터에 앱 표시

./scripts/map-down.sh             # 정리(컨테이너·네트워크 제거, 데이터 볼륨 유지)
```

- 추가 사전 준비: `.env`의 유효한 `GEMINI_API_KEY`, Android 에뮬레이터(`Pixel_7`), Flutter SDK. `AUTH_ENFORCED=true` 로 올릴 때만 `JWT_PRIVATE_KEY`/`JWT_PUBLIC_KEY` 필요
- 이 흐름은 경로 엔진을 올리지 않는다. 도로 모양 경로가 필요하면 `--profile routing up -d osrm-foot osrm-bicycle` 를 따로 낸다(안 올리면 화면의 경로가 직선이 된다). 날씨는 KMA 적재 상태에 따라 빈 배열일 수 있음(정상 동작)
- BFF가 호스트 JVM이라 컨테이너 기동(`--profile full up`)과 토폴로지가 다름에 유의

## 실기기용 외부 노출

이 맥의 스택을 폰에서 쓸 수 있게 여는 흐름이다. `--profile full`(BFF 도 컨테이너)
전제이며, BFF 를 호스트 JVM 으로 띄우는 위 개발 흐름과는 같이 쓸 수 없다 —
관문이 컨테이너 이름으로 BFF 를 찾기 때문이다.

```bash
./scripts/map-serve.sh            # 스택(full+vision+routing) 기동 → 관문 확인 → 터널 개통 → 주소 게시
./scripts/map-serve-down.sh       # 노출만 종료(스택은 유지)
```

- 게시 대상은 `map-service-client/hosting/` 이다. 주소 파일은 `map-serve.sh` 가 그 디렉터리에 직접 쓰므로 이 절차에 사전 빌드가 필요 없다.
- 브라우저로도 쓰려면 웹앱을 따로 올려야 한다. 반드시 `map-service-client/tool/build_web.sh` 로 빌드한다 — `flutter build web` 을 직접 돌리면 서버용 키가 담긴 실행 설정이 자산으로 실려 그대로 공개된다. 배포 전 훅 `check_app_config.sh` · `check_web_secrets.sh` 가 그 경우를 막는다.

- 앱은 시작할 때 고정된 위치에서 현재 서버 주소를 읽는다. 터널 주소가 바뀌면
  이 스크립트를 다시 돌리는 것으로 끝나고, 앱을 다시 만들거나 깔지 않는다.
- 관문(8090)은 여는 경로를 목록으로 못박고 `/actuator` 를 막으며 일정 생성과
  카메라 인식 접속에 IP 당 호출 상한을 건다. 주소를 아는 사람은 누구나 닿을 수
  있으므로 BFF 를 그대로 노출하지 않는다.
- 터널은 원본 응답을 120초까지만 기다린다. 그보다 오래 걸리는 요청은 끊긴다.

## 운영 콘솔 + 모니터링 (관리자 스택)

- **admin(8002)** — 운영 콘솔 JSON API. **admin-web(8003)** — React SPA(nginx,
  `/api` → admin 프록시). 브라우저는 `http://127.0.0.1:8003/` 로 접속(SPA 로그인).
  최초 계정은 `.env` 의 `ADMIN_BOOTSTRAP_USER`/`ADMIN_BOOTSTRAP_PASSWORD` 로 시드된다.
- **모니터링** — `prometheus`(9090)·`grafana`(3000)·`postgres-exporter`·
  `redis-exporter`·`node-exporter`. 관리자 스택 안에서도 `monitoring` 프로파일로
  갈라 두어, 콘솔만 필요할 때는 띄우지 않는다. 전 포트 loopback, 이미지는 multi-arch(arm64 OK).

```bash
# 서비스 스택이 떠 있는 상태에서
./scripts/map-up-admin.sh --monitoring
# Grafana: http://127.0.0.1:${GRAFANA_PORT:-3000}  (GF_SECURITY_ADMIN_USER/PASSWORD, .env)
# Prometheus: http://127.0.0.1:9090
```

- Prometheus 는 hub/agent/admin/yolo `/metrics` 와 user `/actuator/prometheus`,
  exporter 3종을 30s 간격으로 스크레이프한다(보존 10d/2GB — `docker-compose.admin.yml`
  prometheus command 플래그). 서비스 스택이 내려가 있으면 해당 타깃만 down 으로
  표시되고 수집 자체는 계속된다. Grafana 데이터소스·대시보드(서비스 개요·인프라)는
  `monitoring/grafana/provisioning` 으로 코드 프로비저닝된다.
- admin 콘솔의 "모니터링" 화면은 `MONITORING_PANELS`(.env) 로 지정한 Grafana URL 을
  iframe/링크로 임베드한다(Grafana `GF_SECURITY_ALLOW_EMBEDDING=true` + anonymous
  Viewer 전제 — loopback 한정).
- `.env` 추가 키: `GF_SECURITY_ADMIN_USER`, `GF_SECURITY_ADMIN_PASSWORD`, `GRAFANA_PORT`(호스트 포트, 기본 3000).
- `MONITORING_PANELS` 와 `ADMIN_CORS_ORIGINS` 는 JSON 복합 타입이라 **비우려면 키를 주석 처리해야 한다.** `KEY=` 로 빈 값을 남기면 admin 이 `SettingsError` 로 뜨지 못한다.

## 사전 준비

- Docker (Desktop 또는 Engine), `docker compose` v2
- 호스트 RAM 8GB+, 디스크 10GB+ 여유 (`vision` 프로파일을 쓰면 이미지가 커져 여유가 더 필요하고, `routing` 그래프는 볼륨에 7GB 가까이 더 쓴다)
- 호스트 OS 절전·자동업데이트·화면 잠금 비활성화 권장

## License

MIT — see [LICENSE](LICENSE).
