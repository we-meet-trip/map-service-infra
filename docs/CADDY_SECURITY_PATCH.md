# Caddy 공개 TLS 보안 패치 — 2026-09-06

이 작업은 기존 Caddy `v2.11.4`의 표준 모듈과 설정을 유지하면서 Go 툴체인과
명시한 보안 의존성만 바꾸는 **별도 인프라 패치**다. 앱 6개 release manifest,
PostgreSQL, Redis, 관리자 및 모니터링 이미지는 이 패치 대상이 아니다.

## 확인된 원인과 범위

GCP 이미지 `caddy@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648`의
amd64 스캔에서 `/usr/bin/caddy`는 Caddy `v2.11.4`, Go `1.26.3`이다.
보고서는 HIGH 38개, CRITICAL 1개를 기록했다. 이는 패키지별 탐지 건수이며
모두 현재 공개 경로에서 악용 가능하다는 뜻은 아니다.

* **즉시 수정 근거:** [CVE-2026-56862 / GO-2026-6090](https://pkg.go.dev/vuln/GO-2026-6090)는
  악의적인 TLS 클라이언트가 KeyUpdate 메시지로 반복적인 키 파생 연산을
  유발하는 서비스 거부 취약점이다. Go `1.26.6`에서 수정됐다. 현재
  `edge/Caddyfile`과 `docker-compose.edge.yml`은 Caddy에서 공개 443 HTTPS를
  종단하므로 취약 코드가 실제 통신 경로에 있다. HTTP 요청을 받기 전 TLS
  계층에 영향을 주므로 후단 nginx의 HTTP 요청 제한으로 해결되지 않는다.
  실서비스에 공격 트래픽을 보내는 재현은 하지 않았다.
* CRITICAL [CVE-2026-56854 / GO-2026-6303](https://pkg.go.dev/vuln/GO-2026-6303)는
  `x/crypto/ssh.NewServerConn`의 인증 콜백에서 source-address 제약이 누락되는
  문제다. 현재 Caddy 설정에는 SSH 서버가 없다. 이를 HTTPS 인증 우회라고
  해석하지 않는다. 재빌드에서는 해당 의존성도 수정판으로 올린다.
* upstream [최신 Caddy release](https://github.com/caddyserver/caddy/releases/tag/v2.11.4)도
  이 조사 시점에 `v2.11.4`였다. [공식 Dockerfile](https://github.com/caddyserver/caddy-docker/blob/master/2.11/alpine/Dockerfile)은
  해당 release의 미리 빌드된 바이너리를 내려받는다. `apk upgrade`만 수행하면
  Caddy 안에 정적으로 들어간 Go 표준 라이브러리는 바뀌지 않는다.

## 빌드 입력과 증거

`docker/caddy-security/Dockerfile`은 다음 입력을 고정한다.

| 입력 | 고정값 |
|---|---|
| 원본 소스 | `github.com/caddyserver/caddy/v2@v2.11.4` |
| Go | `1.26.8`, `GOTOOLCHAIN=local`, `CGO_ENABLED=0` |
| 공식 Go builder index | `sha256:ce864e7223ac17b1775e6fd0b4c0db580c2eb50e7953a427916379e4b92a1628` |
| builder amd64 manifest | `sha256:6e5de3f5b9fb7e30b8bb2ffe8dcbcbdaa2990f0f31267456eabe83f870a623be` |
| runtime base | 위에 기록한 공식 Caddy 2.11.4 digest |
| `golang.org/x/crypto` | `v0.55.0` |
| `golang.org/x/net` | `v0.57.0` |
| `golang.org/x/text` | `v0.41.0` |
| `google.golang.org/grpc` | `v1.83.1` |

Go `1.26.8`은 [공식 다운로드 목록](https://go.dev/dl/?mode=json)으로 확인했다.
최소 수정 버전 대신 같은 Go 1.26 계열의 현재 패치 버전을 사용한다.
`x/crypto v0.55.0`의 [공식 go.mod](https://github.com/golang/crypto/blob/v0.55.0/go.mod)가
net `v0.57.0`, text `v0.41.0`을 요구하므로 이 조합을 함께 고정한다.

공식 모듈에서 `cmd/caddy/main.go`를 그대로 복사해 Caddy를 의존성으로 빌드한다.
이 방식은 [upstream main.go에 설명된 표준 빌드 방법](https://github.com/caddyserver/caddy/blob/v2.11.4/cmd/caddy/main.go)이며
추가 플러그인이나 Caddy 소스 수정이 없다. Go 모듈은 공식 proxy/checksum DB와
`go mod verify`로 검증한다. 전체 `go.mod`, `go.sum`, 모듈 목록, 원본 모듈 checksum,
`go version -m`, binary SHA256 및 설치 APK 목록이 이미지의
`/usr/share/map-caddy-build/`에 남는다.

runtime의 APK 보안 업데이트는 빌드 시점 저장소를 사용한다. 이후 재빌드에서
APK 결과까지 바이트 단위로 동일하다고 보장하지 않는다. 최종 검증 이미지의
OCI digest 또는 local image ID, 이미지 아카이브 SHA256과 위 증거를 보관하고
그 **동일 산출물**을 배포·복구 대상으로 사용한다.

## 빌드와 검증

현재 CI → release → GCP 배포가 실행 중이면 완료 또는 중단 확인까지 기다린다.
아래 빌드·검증은 live 서비스나 실제 인증서 저장소를 마운트하지 않는다.

```sh
docker buildx build --platform linux/amd64 --load \
  -f docker/caddy-security/Dockerfile \
  -t map-caddy-security:2.11.4-go1.26.8-20260906 .

trivy image --image-src docker --scanners vuln --severity HIGH,CRITICAL \
  --format json --output /tmp/map-caddy-security-trivy.json \
  map-caddy-security:2.11.4-go1.26.8-20260906

python3 -B scripts/verify-caddy-security.py \
  --image map-caddy-security:2.11.4-go1.26.8-20260906 \
  --report /tmp/map-caddy-security-trivy.json
```

Trivy DB 갱신 성공·생성 시각을 함께 보관한다. 원본 Caddy 이미지도 비교 실행을
위해 로컬에 있어야 한다. 검증 스크립트는 image ID와 amd64 스캔의 일치,
설치 바이너리 hash, 정확한 Go/Caddy/보안 의존성 버전, 기존과 동일한 표준 모듈,
HIGH/CRITICAL 0개를 확인한다. 컨테이너는 `--network none`, read-only rootfs,
tmpfs만 사용해 실제 HTTP 및 임시 내부 CA의 HTTPS 부팅을 확인한다.
공개용 Caddyfile도 합성 도메인·이메일로 validate한다. 테스트 HTTPS의
`curl -k`는 이 일회용 내부 CA 점검에만 사용한다. GCP 공개 TLS 검증에는 쓰지 않는다.

```sh
# Docker나 네트워크를 사용하지 않는 검증기 회귀
python3 -B scripts/verify-caddy-security.py --self-test
```

## GCP 교체 순서와 완료 조건

실제 교체는 별도 권한을 가진 배포 작업자가 수행한다. 이 문서나 검증기는
원격 서버를 변경하지 않는다.

1. 자동 배포 프로세스가 종료됐는지 확인하고 같은 배포 lock을 확보한다.
   진행 중 자동 배포와 병렬로 edge를 재생성하지 않는다.
2. 기존 edge 컨테이너·이미지 ID, Caddyfile SHA256, Compose project, 인증서
   볼륨 이름을 기록한다. 인증서 `/data`, `/config`는 삭제하거나 새 볼륨으로
   대체하지 않는다. 검증된 새 이미지 아카이브와 SHA256을 GCP로 전달하고
   load 후 image ID를 대조한다.
3. 현재 배포의 앱 pin과 인프라 pin을 유지한 상태에서 **edge에만** 새 image ID와
   `pull_policy: never`를 마지막 override로 적용한다. 렌더링된 Compose에서
   나머지 서비스의 이미지·볼륨·설정이 그대로인지 확인한다.
4. `up -d --no-deps --no-build --pull never edge`로 edge만 교체한다.
   기존 PostgreSQL·Redis 및 앱 컨테이너 ID의 불변을 확인한다.
5. 공개 HTTPS 인증서 체인 검증, HTTP→HTTPS redirect, `/healthz`,
   `/healthz/app`, 무인증 `/api/v1/users/me` 401, `/actuator` 차단,
   Vision/Chat WebSocket 핸드셰이크, 합성 CF-Connecting-IP 위조 헤더가
   덮어써지는 동작을 확인한다. 오류 로그·메모리·CPU는 값/건수만 기록한다.
6. 실패하면 같은 설정·볼륨을 사용해 **edge만** 기록한 이전 image ID로
   한 번 복구한다. 이 경우 보안 패치는 미완료이며 이전 TLS 취약점이 남았다고
   기록한다. DB downgrade, `compose down`, volume 삭제를 하지 않는다.
7. 성공한 새 edge image ID와 검증 보고서를 인프라 보안 패치 증거로 보관한다.
   다음 앱 자동 배포는 이미 설치된 non-app image ID를 보존하므로 새 edge를
   유지한다. 새 호스트를 구축하는 NCP 작업에도 이 검증된 산출물을 명시적으로
   반영해야 한다. 기본 upstream tag로 재설치하면 이 패치가 사라질 수 있다.

빌드·스캔·격리 부팅·공개 경로 확인이 모두 끝나기 전에는 수정 완료로 판단하지 않는다.

## 검증된 전달 artifact를 사용하는 신규 호스트 설치 (2026-09-07 KST)

`docker/caddy-security/install-artifact-20260906.json`이 검토한 archive와 Trivy 보고서의 신뢰 기준이다. archive SHA256 `c85c7192e92644fccd75d86bdfca599425603a647e77183b29effaddfe697fcc`, 68,931,584 bytes, linux/amd64 config image `sha256:5d27f970d3a1c93e77bd28089bf3294ef40b9f7832648419b8a3c2b43b4bc670`이다. 이 기준 파일은 새 빌드 검토 없이 바꾸지 않는다. 현재 파일은 작업 호스트의 `/tmp/map-caddy-security-image-20260906.tar`와 `/tmp/map-image-audit-20260906/caddy-security-20260906.json`이다. NCP 생성 전 별도 안전한 보관소로 전달하고 원격에서 다시 검증한다.

```sh
python3 scripts/install-caddy-artifact.py \
  --archive /secure-transfer/map-caddy-security-image-20260906.tar \
  --report /secure-transfer/caddy-security-20260906.json \
  --install-compose /secure-transfer/compose.caddy-installed.yml
EDGE_IMAGE_OVERRIDE=/secure-transfer/compose.caddy-installed.yml \
  bash scripts/cloud-up.sh --registry --edge --vision
```

첫 명령은 checksum 및 OS/아키텍처·플랫폼 image ID·설치된 binary hash를 확인하고, network-none 임시 컨테이너의 공개 설정 검사와 실제 HTTP/TLS 기동을 통과해야 `pull_policy: never` override를 작성한다. 존재하는 override는 덮어쓰지 않는다. 둘째 명령은 override가 검토된 이미지인지 다시 확인한다. artifact 없이 신규 edge를 mutable 태그에서 당기는 자동 receiver 경로는 거절한다. 기존 GCP edge는 이전 컨테이너에서 캡처한 동일 이미지로 보존된다.

2026-09-07 로컬 실행에서 실제 archive import와 HTTP/TLS smoke PASS를 확인했다. 이미 Docker가 설치된 로컬 호스트에서 실행한 증거이며, 빈 NCP VM의 OS/bootstrap/네트워크/공개 인증서 설치 완료를 의미하지 않는다. 설치 스크립트 자체는 기존 컨테이너·볼륨을 변경하지 않는다. serving 전환은 이전 image ID와 Compose/env를 별도로 보관한 후 수행하고 실패 시 그 override로 복귀한다. 이 artifact와 검사 시점 이후 advisory 재검토는 별도 보안 gate다.
