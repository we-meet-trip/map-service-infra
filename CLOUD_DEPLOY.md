# 클라우드 배포와 인수인계

갱신: 2026-09-06. 이번 승인 범위는 **브랜치 작업 완료 → GCP 시험 서버 검증**이다.
NCP 운영 서버는 아직 생성하지 않았다. 아래 NCP 항목은 후속 인수 절차이며,
코드 구현·자격증명 준비·실제 앱 배포의 완료 여부를 구분한다.
워크스페이스의 `MAP_RELEASE_EXECUTION_2026-09-06.md`가 실행 증거 원장이고,
`NCP_MIGRATION_RUNBOOK.md`가 NCP 단계별 절차다. 이전 계획의 수동 태그 교체·
4이미지·시험 환경 전체 스텁 설명 대신 현재 코드와 아래 계약을 적용한다.

## 1. 확인된 상태와 다음 게이트

| 항목 | 2026-09-06 확인 상태 |
|---|---|
| GCP 시험 앱 | `mapcenter-b59ca / us-central1-a / map-test`, 기존 판 `2026-09-06-5`의 9컨테이너 healthy. 이번 수정 앱의 배포·재시작은 아직 미실행 |
| GCP 접속 | IAP SSH 및 sudo 확인. 자동배포 계정 `mapdeploy`는 고정 명령만 허용, 호스트 키 고정 및 잘못된 입력 거절 실증 |
| GCP 백업 | 비공개 `mapcenter-b59ca-test-backups`에 역할·DB·manifest 저장 및 재다운로드 SHA256 검증. 30분 systemd timer 활성화, 2026-09-06 04:00 UTC 자동 실행 성공 확인 |
| DB 복원 | GCP 사본을 network none 새 컨테이너에 복원하고 31테이블 행 수 대조 통과(14.08초). 전체 앱 RTO 실증과는 구분 |
| 이미지·자동배포 | 여섯 이미지의 SHA/digest 묶음과 수령자 코드 구현. 새 CI → image-release → 실제 GCP 앱 반영의 전체 성공 확인은 남음 |
| 중앙 관리자 | 환경별 연결·개인 계정·권한·준비 상태·감사 기능 로컬 시험 완료. GCP 실제 배포 및 비공개 접근 실증은 남음 |
| NCP | 자원 생성·운영 DNS 전환·S3 실전송·운영 배포 모두 후속 |

공개 시험 배포에서도 실제 장소를 사용하며 `AUTH_ENFORCED=true`,
`PLACES_STUB_MODE=false`를 유지한다. 학습 수집·내보내기는 HOLD다.
학습 비활성화와 일정·채팅 등 서비스 데이터의 정상 저장을 혼동하지 않는다.
`make-test-env.sh`의 격리 시험 fixture를 실제 외부 API·인증 자격증명 검토 없이
공개 서버에 그대로 쓰지 않는다.

## 2. 환경과 네트워크

운영은 NCP, 시험은 GCP로 분리한다. 중앙 비공개 관리자 콘솔은 하나만 두고
`ADMIN_TARGETS`에 서버 측 대상 연결과 권한을 등록한다. 시험·운영 DB, Redis,
JWT/암호화 키, 내부 토큰, DNS 권한, 백업 경로를 분리한다. NCP에 두 번째
admin/admin-web을 만드는 절차는 사용하지 않는다.

| 연결 | 정책 |
|---|---|
| 외부 → edge | 80/443만 서비스 공개 |
| 운영자 → GCP SSH | IAP 및 제한된 IAM. 자동배포는 고정 목적지·강제 명령·호스트 키 확인 |
| 운영자 → NCP SSH | 지정 운영자 출발지 또는 승인된 사설 접속 경로만 허용 |
| DB/Redis/proxy/OSRM/admin/지표 | 직접 공개하지 않는다. 호스트 바인딩은 loopback, 필요한 내부 연결만 허용 |
| 서버 egress | 외부 HTTPS/HTTP API 외에 DNS의 UDP/TCP 53, 시간 동기화 방식에 따른 NTP 등을 확인. 80/443만 열어 완료로 보지 않는다 |

proxy의 요청자 IP 계약은 Caddy와 nginx 설정을 함께 검증한다. edge는 외부에서
받은 `CF-Connecting-IP`를 자신이 관찰한 주소로 덮어쓴다. 같은 이름을 삭제·설정하는
중복 조합은 제거했다. proxy 직접 노출을 막고 신뢰하는 내부 송신자 범위를 제한한다.
배포 후 서로 다른 클라이언트의 IP 및 제한 버킷 분리, 위조 헤더 무효화를 확인한다.
429의 유무만으로 판정하지 않는다. 보존 로그의 `INFO ... transport error` 문자열도
ERROR로 세지 않는다.

중앙 관리자 시험 포트는 UI `8203`, API `8202`, Grafana `13200`이다
([시험 오버레이](docker-compose.admin.test.yml)). 모두 loopback에 바인딩한다.
운영 지표의 Grafana 포트는 `GRAFANA_PORT` 값(기본 3000)이다.
`MONITORING_PANELS`에는 운영자 브라우저가 접속할 로컬 터널 URL을 넣는다.
Secure 쿠키는 HTTPS에서 사용한다. `ADMIN_SESSION_COOKIE_SECURE=false`는
승인된 localhost HTTP SSH 터널의 시험 설정으로 한정하고 공개 HTTP에 적용하지 않는다.

## 3. 배포 묶음과 자동화

[image-release](.github/workflows/image-release.yml)는 infra/user/agent/hub/yolo/admin의
소스 SHA를 고정한 뒤 검사하고, user/hub/agent/yolo/admin/admin-web 여섯 이미지를
발행한다. client는 서버 이미지 묶음에 포함하지 않으며 자체 CI·release를 사용한다.
`ref` 기본값은 `develop`이며 수동 입력 branch/tag는 여섯 소스 저장소에 모두 있어야 한다.
SHA를 순서대로 캡처하므로 여러 저장소의 병합이 원자적으로 수행되는 것은 아니다.
통합할 변경을 먼저 모두 병합·검사한 다음 릴리스의 소스 SHA 목록을 확인한다.

현재 자동 트리거는 **infra develop push → infra ci 성공 → image-release →
deploy-gcp-test**다. user/agent/hub/yolo의 develop CI가 보내는 선택적
`repository_dispatch: release-develop` 경로도 구현했다. 네 저장소의
`RELEASE_DISPATCH_TOKEN`이 없으면 송신 단계만 건너뛰며, 일반 CI는 계속 동작한다.
현재 토큰은 등록하지 않았으므로 이 경로의 자동 실행은 아직 활성화하지 않았다.

사용자가 한 번 준비할 fine-grained PAT은 대상 저장소를 **map-service-infra만**
선택하고 **Contents: write** 권한을 부여한다. repository dispatch 생성에 필요한
권한은 Actions: write가 아니다([GitHub REST 공식 문서](https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event)).
이 값을 네 송신 저장소의 `RELEASE_DISPATCH_TOKEN` secret으로 등록한다. 수령자는
공개 원본 저장소의 CI를 읽기 전용 API로 확인하므로 추가 PAT를 사용하지 않는다.
토큰 발급·등록·권한 변경은 이 구현에 포함하지 않았다.

수령 payload는 `repository`, `sha`, `run_id` 문자열 세 개만 허용한다. 고정된
네 저장소의 `.github/workflows/ci.yml` 실행이 `push/develop`, 동일 저장소·SHA이며
최종 성공인지 검사한다. 송신 job이 같은 CI에 속하므로 최대 120초 동안 완료를
기다린다. 성공 SHA가 현재 develop과 다르면 중단하며, payload의 임의 ref나 URL로
checkout하지 않는다. 검증한 원본 서비스만 정확한 SHA로 고정하고 나머지 저장소는
develop 스냅샷을 사용한다. manifest의 provenance에는 원본 CI 근거를 담고 해당
서비스 이미지의 SHA와 일치시킨다. 이미지 묶음 생성 및 deploy 준비 단계에서도
CI 성공·현재 develop을 재검증하므로 빌드 중 새 커밋으로 넘어갔으면 새 CI가 필요하다.
검증된 dispatch 릴리스는 자동 배포 대상에 포함된다. API 실패·제한·완료 기한 초과는
검증을 건너뛰지 않고 릴리스를 실패시킨다.

수동 image-release는 자동 배포하지 않으며, 성공 run ID를 `deploy-gcp-test`에
명시하는 별도 수동 실행 경로가 있다. 자동 체인은 workflow가 기본 브랜치에 존재하고
관련 CI와 환경 정책이 준비된 상태에서 실제 한 번 끝까지 관찰해야 완료다.

릴리스 태그는 식별용이며 배포 기준은 OCI `@sha256:`이다. 배포 artifact는
`release.json`, `compose.images.yml`, `compose.admin-images.yml`, `SHA256SUMS`로
구성된다. [검증기](scripts/release_manifest.py)는 파일 집합·크기·해시·소스 SHA·
이미지 digest·OCI 라벨을 검사한다. [수령자](scripts/deploy-gcp.py)는 추가로
GitHub 저장소/워크플로/run의 성공과 artifact 출처·해시를 검사한다.
임의 태그를 `.env.test`에 적는 방법으로 이 검증을 우회하지 않는다.

[deploy-gcp-test](.github/workflows/deploy.yml)는 GitHub `gcp-test` environment를
사용한다. 현재 인증은 제한된 SA의 JSON 키와 별도 SSH 키이며 **WIF 전환은 미구현**이다.
키를 앱 환경파일·이미지·로그에 넣지 않고 전용 저장소의 접근 권한·회전·회수를 관리한다.
GitHub environment 이름 존재가 검토자 승인·브랜치 보호 정책 설정까지 증명하지는 않는다.

수령자는 고정 GCP 인스턴스 확인, 배포 잠금, 기존 infra/env/이미지 기록, 사전 원격
백업, migration 및 readiness 검사, 실제 API smoke를 순서대로 수행한다.
실패 시 한 번의 앱 복귀를 시도하며 복귀 성공도 해당 배포의 성공으로 처리하지 않는다.
새 코드가 이 경로로 서버에 실제 반영됐는지는 run 결과와 서버 digest를 대조해 확인한다.

## 4. 환경파일과 기동 계약

환경파일은 `.env.example`를 기준으로 만들되 기존 비밀값을 임의로 덮어쓰지 않는다.
값을 출력하지 않고 필수 키 존재·형식과 compose 조합을 검증한다. 운영 설정에는
`TESTER_SEED_ENABLED=false`, `AUTH_ENFORCED=true`, `PLACES_STUB_MODE=false`,
`TRAINING_CAPTURE_ENABLED=false`, `TRAINING_EXPORT_ENABLED=false`를 명시한다.
`VISION_INTERNAL_TOKEN`은 user와 YOLO가 공유하는 전용 비밀이며 다른 내부 토큰과
분리한다. JWT·위치 암호화 키·외부 API 키와 관리자 대상 연결도 환경별로 준비한다.
관리자 시험 오버레이는 `MAP_STACK_ENV` sentinel과 `.env.test`를 요구한다.

서버에는 infra 소스와 검증된 이미지 묶음을 둔다. registry 모드는 migration을
이미지 안의 코드로 수행하므로 이웃 `../map-service-hub` 소스 checkout을 요구하지 않는다.
`RELEASE_BUNDLE`을 쓸 때는 `--registry`가 필수다. `cloud-up.sh`의 묶음 파일 존재
확인은 GitHub artifact 출처 확인을 대체하지 않는다. 수령자 또는 별도 검증기를 먼저
통과해야 한다. GCP 자동 수령자의 기능 조합은 다음과 같다.

```text
--test --registry --vision --edge --admin --monitoring
```

NCP 후속 조합은 `--registry --vision --edge --routing`이다. 중앙 admin을 추가로
기동하지 않는다. 서비스가 먼저 네트워크·DB를 준비하고 중앙 관리자 연결이 그 뒤에
검증되어야 한다. 기동 스크립트 완료는 smoke나 실기기 기능 검사의 대체물이 아니다.

Hub migration 오류는 즉시 실패 처리하고 실제 revision이 기대한 단일 head와 일치해야
한다. User Flyway와 관리자 migration도 함께 확인한다. health가 초록이라는 이유만으로
테이블·컬럼·권한 정합을 추정하지 않는다.

## 5. 경로 그래프와 자원 판정

NCP의 8GB RAM/100GB 디스크는 후속 시험의 출발점이며 용량 적합성의 확정값이 아니다.
발행 이미지와 서버 아키텍처를 대조한다. OSRM의 mmap도 페이지 캐시와 동시 요청에
따라 메모리를 사용하므로 “추가 메모리가 필요 없다”고 계산하지 않는다.
모델·앱 이미지·두 경로 그래프·로그·백업 임시 공간을 디스크 예산에 넣는다.
`--micro`의 제한은 출시 부하 시험 기준으로 사용하지 않는다.

OSRM은 `osrm-data` 볼륨의 **전체 산출물**을 엔진 버전·원본 시각·SHA256과 함께
보관·전송한다. edges/partition/cells/mldgr 여덟 파일 존재 검사는 최소 누락 검사다.
foot/bicycle 양쪽에서 `code=Ok`, 실제 도로 경로와 이동 수단, 부하 중 메모리·OOM·
지연을 확인한다. 좌표가 두 개라는 사실만으로 stub이라고 단정하지 않는다.
재빌드 가능하다는 이유로 복구 사본을 생략하지 않는다. 재생성 시간이 RTO를 넘을 수 있다.

## 6. 백업과 복구

`scripts/pg-backup.sh --test` 또는 `--prod`로 환경을 명시한다. DB dump와 비밀번호를
제외한 역할 정의, SHA256 manifest가 한 벌이며 manifest를 마지막에 전송한다.
`BACKUP_REQUIRE_REMOTE=1`일 때 원격 미설정·전송 실패는 실패 상태로 남긴다.

| 원격 형식 | 현재 검사 | 인수 조건 |
|---|---|---|
| SCP `user@host:/absolute/path` | 원격 SHA256 대조 | SSH 대상·호스트 키·권한 확인 |
| GCS `gs://bucket/prefix` | 재다운로드 SHA256 대조 | GCP 시험에서 실제 성공 및 timer 실행 확인 |
| S3 `s3://bucket/prefix` | 업로드 후 ContentLength 대조 | NCP endpoint/최소 권한 설정 후 재다운로드 SHA256 및 복원 실증 필요 |

S3의 크기 일치는 checksum 검증 완료가 아니다. `BACKUP_S3_ENDPOINT`와 전용 CLI
자격증명을 호스트에 분리하고 앱 `.env`에 넣지 않는다. GCP 백업 SA도 전용 비공개
버킷의 objectCreator/objectViewer만 사용하며 기존 VM의 OAuth scope를 바꾸지 않았다.

```bash
# 동일 작업 디렉터리에 manifest와 companion 두 파일을 준비한다.
./scripts/pg-restore-check.sh --test /path/to/backup.manifest.json
```

복원 검사는 기존 볼륨을 붙이지 않는 network none의 새 컨테이너에서 수행한다.
역할 비밀번호·JWT·암호화 키는 별도로 보존해야 한다. DB 복원과 행 수 대조만으로
암호문 복호화·로그인·앱 전체의 RTO 4시간을 달성했다고 보고하지 않는다.
RPO 1시간은 원격 성공 시각으로 측정한다. 30분 timer 활성화 외에 마지막 성공이
1시간을 넘을 때의 알림·보존 정책·정기 복구 훈련을 인수해야 한다.

롤백은 같은 환경의 이전 이미지 digest·infra SHA·설정·활성 profile을 함께 복원한다.
자동 수령자는 DB downgrade나 볼륨 삭제를 하지 않는다. 관리자 기존/신규 head 차이는
자동 복귀 호환성 검토 전 차단한다. 모든 User/Hub schema 변경이 자동으로 안전하다는
뜻은 아니므로 비호환 변경은 forward fix 또는 검증된 DB 복구 절차를 준비한다.
GCP 시험 DB로 운영 이용자를 보내는 것은 운영 복구가 아니다.

## 7. 앱 설정과 최종 인수

앱은 빌드의 `APP_ENV`, `API_ALLOWED_ORIGINS`, `APP_CONFIG_URL`로 환경 신뢰 범위를
정한다. 설정 URL에서 받은 API 주소도 허용된 HTTPS origin이어야 한다. 같은 환경의
허용 범위 안에서만 다음 실행 시 주소 변경이 가능하다. 새로운 origin이나 시험→운영
전환은 해당 빌드 설정과 릴리스가 필요할 수 있다. GCP 시험 설정 파일을 운영 주소로
덮어쓰지 않는다. 초대 링크의 검증 파일과 앱 식별자/서명도 별도로 확인한다.

client는 `.env` 자산과 `flutter_dotenv`를 제거했다. Google Maps는 Android/iOS/Web별
제한된 클라이언트 키를 주입하며 서버 Places 키를 앱으로 복사하지 않는다. 웹 키가
보이는 것은 SDK 사용의 특성이므로 referrer/API 제한을 검증한다. 키 없는 컴파일 성공을
지도 인증 성공으로 보지 않는다. Apple Developer 설정·서명·실기기 로그인도 별도 게이트다.

최종 GCP 인수에서는 릴리스 원본 SHA/digest 일치, 인증 없는 요청 거절, 실제 장소·수동
편집 순서·채팅 참가 구간·Vision permit·탈퇴·중앙 관리자 권한과 장애 표시, 실제 IP별
제한, 새 로그의 심각 오류, 배포 실패 경로를 확인한다. 보존 로그 전체의 읽기 전용
기준선은 워크스페이스 `evidence/runtime/local-retained-20260906.jsonl`에 있다.
NCP는 이 결과를 인수한 후 DNS 소유권·계정 분리·최신 요금·자원·원격 복구·운영 앱 설정을
확정하고 별도 실행한다.
