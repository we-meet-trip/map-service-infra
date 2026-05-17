# map-service-infra

MAP 서비스의 인프라 오케스트레이션 레포. 다른 4 레포(agent · client · hub · user) 위에 위치하며 컨테이너 구성·환경변수·DB 초기화·OSRM 빌드 스크립트를 단독 보유한다.

## 역할

- `docker-compose.yml` — PoC 단일 호스트 오케스트레이션 (7 services × 3 profiles)
- `.env.example` 단일 진실원 — 모든 service에 `env_file: ./.env`로 주입
- `db/init/00-create-schemas.sql` — postgres 첫 부팅 시 schema 3개(`user_service`, `hub_data`, `langgraph`) 생성 + grant
- `scripts/osrm-rebuild.sh` — OSRM PBF 빌드 (수동 실행, 월 1회 권장)
- `docs/` — 향후 OpenAPI spec · UML 다이어그램 · 인계 문서 보관 위치

## 폴더 구조

```
map-service-infra/
├── .env.example                  환경변수 템플릿
├── docker-compose.yml            7 services + 3 profiles
├── db/
│   └── init/
│       └── 00-create-schemas.sql 첫 부팅 시 자동 실행
├── docs/
│   ├── api/                      OpenAPI spec 보관
│   ├── diagrams/                 PlantUML/Mermaid 다이어그램
│   └── handoff/                  페어 셰도잉 인계 문서
└── scripts/
    └── osrm-rebuild.sh           OSRM PBF 빌드 스크립트
```

## profiles

| profile | 포함 service |
|---|---|
| `infra` | postgres · redis · osrm-foot · osrm-bicycle |
| `backend` | user · agent · hub |
| `full` | 위 전부 |

## 실행 (macOS · Windows WSL2 · Linux 공통)

```bash
cd map-service-infra
cp .env.example .env                              # API 키 주입
./scripts/osrm-rebuild.sh                         # 1회, 30-60분, RAM 6GB+
docker compose --profile full up -d --build

# 검증
curl -s http://127.0.0.1:8080/actuator/health     # user
curl -s http://127.0.0.1:8000/health              # agent
curl -s http://127.0.0.1:8001/health              # hub
docker compose exec postgres pg_isready -U map
docker compose exec redis    redis-cli ping
docker compose exec postgres psql -U map -c "\dn" # 3 schemas
```

## 사전 준비

- Docker (Desktop 또는 Engine), `docker compose` v2
- 호스트 RAM 8GB+, 디스크 10GB+ 여유
- 호스트 OS 절전·자동업데이트·화면 잠금 비활성화 권장

## License

MIT — see [LICENSE](LICENSE).
