# GCP 관리자와 NCP 운영 API 연결

GCP의 기존 `admin` 컨테이너에만 별도 내부 Docker bridge를 붙이고, 해당 bridge의 호스트 gateway에 SSH local forward 세 개를 연다. 기존 GCP 시험 네트워크·관리자 계정 DB·Redis는 유지한다. NCP PostgreSQL·Redis 연결은 만들지 않는다.

| 관리자 대상 | NCP SSH 연결 목적지 | 인증 입력 |
|---|---|---|
| User | `127.0.0.1:8080` | `USER_ADMIN_INTERNAL_TOKEN` |
| Agent | `127.0.0.1:8000` | `INTERNAL_SERVICE_TOKEN` |
| Hub | `127.0.0.1:8001` | `HUB_ADMIN_INTERNAL_TOKEN` |

이 포트는 `docker-compose.prod.yml`이 재사용하는 실제 서비스 loopback 바인딩이다. NCP 서버 IP, GCP의 실제 외부 송신 IP, bridge subnet·gateway, GCP의 빈 local port 세 개는 배포 시 조회한 값을 사용한다. 문서의 변수에 임의 주소를 대입하지 않는다.

## 입력 확인과 준비

1. GCP에서 Docker 네트워크 subnet과 호스트 라우팅 테이블을 읽고 충돌하지 않는 private subnet·gateway를 정한다. `ss -ltn`으로 선택한 local port 세 개가 비어 있는지 확인한다. `map-admin-ncp-tunnel` 네트워크가 이미 존재하면 `Internal=true`, subnet·gateway와 연결 컨테이너를 확인하여 재사용 여부를 판단한다.
2. 현재 GCP 관리자 컨테이너의 `ADMIN_TARGETS`를 값 출력 없이 권한 `0600` JSON 파일에 저장한다. `{}`는 실제 설정이 비어 있음을 확인한 경우에만 사용한다. 기존 `test` 등의 대상은 준비 도구가 그대로 보존한다. 다른 `prod` 대상이 이미 있으면 자동으로 덮어쓰지 않는다.
3. NCP 배포에 검증·사용하는 운영 runtime `.env`와 별도 SSH 개인키를 준비한다. 도구는 `APP_ENV=prod`와 서로 다른 세 토큰을 요구하고 비밀 입력 파일 권한을 검사한다. 개인키는 비대화형 전용 키이며 일반 관리용 키를 재사용하지 않는다.
4. NCP의 관리 콘솔 또는 이미 검증된 관리 연결에서 SSH host key fingerprint를 확인한다. 준비할 `known_hosts`의 같은 IP·포트 키와 대조한다. `ssh-keyscan` 결과만으로 서버 신원을 확인한 것으로 처리하지 않는다.

```bash
python3 scripts/prepare-ncp-admin-tunnel.py \
  --ncp-host "$MAP_NCP_IP" --ncp-ssh-user "$MAP_NCP_TUNNEL_USER" \
  --gcp-source-ip "$MAP_GCP_EGRESS_IP" \
  --bridge-cidr "$MAP_ADMIN_BRIDGE_CIDR" --bridge-gateway "$MAP_ADMIN_BRIDGE_GATEWAY" \
  --local-user-port "$MAP_ADMIN_USER_PORT" \
  --local-agent-port "$MAP_ADMIN_AGENT_PORT" \
  --local-hub-port "$MAP_ADMIN_HUB_PORT" \
  --production-env-file "$MAP_PRODUCTION_ENV_FILE" \
  --current-admin-targets-file "$MAP_CURRENT_ADMIN_TARGETS_FILE" \
  --identity-file "$MAP_ADMIN_TUNNEL_IDENTITY" \
  --known-hosts-file "$MAP_VERIFIED_NCP_HOSTS" \
  --output-dir "$MAP_ADMIN_TUNNEL_OUTPUT"
```

SSH 포트가 기본 22와 다르면 `--ncp-ssh-port`를 명시한다. 출력 디렉터리는 새 경로여야 한다. 이 명령은 private 파일만 준비하며 서버·Docker·systemd·계정을 변경하지 않는다. `plan.json`의 `PREPARED_NOT_DEPLOYED`는 실제 연결 성공을 뜻하지 않는다.

## NCP 설치

- root가 아닌 전용 SSH 계정을 만든다. 일반 대화형 관리자 계정의 설정이나 키는 변경하지 않는다. 준비한 `ncp-authorized_keys`는 그 계정의 `~/.ssh/authorized_keys`에 설치하고 디렉터리 `0700`, 파일 `0600`, 소유자를 해당 계정으로 지정한다.
- 준비한 `ncp-sshd.conf`를 별도 sshd 설정 조각으로 설치한다. `restrict,port-forwarding`에 GCP 송신 IP와 세 `permitopen`만 적용하며 sshd의 해당 사용자 `Match`에서 `AllowTcpForwarding local`, `PermitListen none`, `ForceCommand /usr/sbin/nologin`을 적용한다. 따라서 키로 셸이나 역방향 포워딩을 열지 않는다.
- 기존 관리 SSH 연결을 유지한 채 **NCP에서 `sshd -t`와 `sshd -T -C user=...,addr=...`를 실행하여 문법·실제 적용값을 확인한 뒤 reload**한다. 전용 계정의 공개키 인증이 작동하는지도 확인한다.
- NCP ACG의 SSH 접근은 실제 GCP 송신 IP에 허용한다. User·Agent·Hub는 loopback에만 노출한다. SSH가 연결하는 Docker published port에서 서비스가 관측하는 source 주소를 확인하고 기존 User/Hub의 trusted CIDR 규칙과 대조한다. 미확인 대역을 넓혀 추가하지 않는다.

## GCP 설치와 관리자 반영

GCP에 다음 내부 네트워크를 준비한다. 기존 이름이 존재하면 먼저 위의 실제 설정을 확인하며 삭제·재생성하지 않는다.

```bash
docker network create --driver bridge --internal \
  --subnet "$MAP_ADMIN_BRIDGE_CIDR" --gateway "$MAP_ADMIN_BRIDGE_GATEWAY" \
  map-admin-ncp-tunnel
```

전용 로컬 시스템 사용자·그룹 `map-admin-tunnel`을 준비한다. SSH용 `identity`, `known_hosts`, `ssh_config`를 `/etc/map-admin-ncp/`에 권한 `0600`, 소유자 `map-admin-tunnel`로 설치한다. 설치 디렉터리는 root 소유 `0755`로 두고 각 비밀 파일의 `0600` 권한으로 접근을 제한한다. `admin-targets.env`는 같은 위치에 root 소유 `0600`으로 설치한다. 준비한 `compose.yml`은 같은 위치에 root 소유 `0644`로 설치한다. 이 파일은 private env의 경로만 담으며 재배포 시 연결 유지 여부를 판별하는 호스트 설정이다. 관리자 컨테이너에는 이 파일의 `ADMIN_TARGETS` 값만 전달하며 SSH 개인키를 mount하지 않는다.

`deploy/map-admin-ncp-tunnel.service`를 systemd에 설치하여 네트워크 생성 후 시작한다. SSH는 확인된 host key만 허용하며 인증 실패·bind 충돌 시 종료한다. 재연결은 systemd가 담당한다.

현재 GCP 관리자 배포에 사용하는 **동일한 env 파일·동일한 registry 이미지 오버레이·동일한 프로젝트**를 유지하고 마지막에 `docker-compose.admin.ncp.yml`만 추가한다. 실제 기존 배포가 아래와 같은 조합일 때의 예다.

```bash
NCP_ADMIN_TARGETS_ENV_FILE=/etc/map-admin-ncp/admin-targets.env \
  docker compose --env-file .env.test \
  -f docker-compose.admin.yml -f docker-compose.admin.test.yml \
  -f docker-compose.admin.registry.yml -f docker-compose.admin.ncp.yml \
  up -d --no-deps --no-build admin
```

`ADMIN_TARGETS.prod`에는 세 API URL·세 독립 토큰과 빈 `ADMIN_DATABASE_URL`, `ADMIN_REDIS_URL`만 들어간다. `format: raw` env 파일을 사용하고 기존 Compose `environment.ADMIN_TARGETS`를 reset하여 값의 우선순위를 명확히 한다. **일반 `docker compose config`·컨테이너 inspect 결과는 토큰이 포함될 수 있으므로 터미널·증거 파일에 원문을 출력하지 않는다.** 렌더 검사에서 필요한 비교 결과만 기록한다.

`deploy-gcp.py`와 `cloud-up.sh`는 `/etc/map-admin-ncp/compose.yml`이 설치되어 있으면 항상 추가하여 후속 관리자 재생성에도 연결을 유지한다. receiver는 이 기능의 marker가 없는 과거 checkout을 거부하므로 rollback이 NCP 대상을 조용히 제거하지 않는다. `map-up-admin.sh`는 기본 map-net만 아는 로컬 진입점이므로 이 호스트 설정이 있는 GCP에서는 실행을 거부하고 시험 배포 경로를 안내한다. Docker 재시작은 저장된 컨테이너 network와 env 구성을 재사용한다. 임의의 직접 Compose 명령에도 위의 마지막 오버레이가 필요하다. 다른 모니터링 컨테이너는 새 bridge에 연결하지 않는다.

## 실제 인수 검사

- Docker network inspect에서 `Internal=true`, 정확한 subnet·gateway와 **현재 admin 컨테이너만** 연결되어 있음을 확인한다. `ss -ltnp`에서 세 forward가 bridge gateway에만 열리고 `0.0.0.0`에 열리지 않았는지 확인한다.
- 관리자 로그인·기존 `test` 대상이 그대로 동작하고 `prod` 대상의 User 조회·신고 처리와 Hub/Agent 요청이 성공하는지 검증한다. 각 관리자 요청은 해당 토큰을 사용하며 잘못된 토큰은 거부되어야 한다.
- NCP DB·Redis 진단은 API-only 대상에서 `not_configured`이며 GCP 데이터에 대신 접근하지 않아야 한다. API-only 지원은 기존 `map-service-admin/app/config.py`를 재사용한다.
- 터널을 중지하면 NCP 관리자 대상 요청만 실패하고 GCP 관리자 로그인·시험 대상 및 NCP 일반 앱 요청은 계속 동작해야 한다. 복구 후 다시 조회한다.
- 실패 시 이 systemd 서비스만 중지하고 기존 관리자 Compose 조합으로 관리자 컨테이너만 복구한다. 데이터베이스·Redis·볼륨·기존 네트워크를 삭제하지 않는다.
