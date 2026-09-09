# NCP 빈 호스트 설치 계약

2026-09-09 현행 배치: **기존 GCP는 테스트와 중앙 관리자, 신규 NCP는 운영만**. 네 서버 분리와 신규 관리자/학습 VM은 HOLD다. 아래 2026-09-07 비용 비교는 역사적 검토이며 현행 견적이 아니다.

2026-09-07, 세션 A. 이 문서와 `deploy/ncp-bootstrap/`, `scripts/ncp-bootstrap-*`만 신규 bootstrap 소유 범위다. 기존 GCP, 공용 Compose, root/session C receiver, 기반 보안 빌드 소스는 변경하지 않는다. 준비한 도구는 호스트·파일·이미지 cache까지만 담당하며 애플리케이션·receiver를 실행하지 않는다. 유료 생성·운영 전환·DNS와 develop/master 병합은 사용자 확인 전 HOLD다.

## 선정한 설치 프로필

| 역할 | 공급자·사양 | root / data | 계정·데이터·실행 경계 |
|---|---|---|---|
| 시험 | 기존 GCP us-central1-a e2-medium 유지 | 기존 디스크 보존 | 기존 OS/daemon/시험 DB·secret 보존, 도구에서 test 설치 거절 |
| 운영 | NCP 한국 VPC s2-g3, 2 vCPU/8GB 우선 | CB2 40GB / 100GB | `map-deploy-prod`, `/srv/map-prod`, User·Agent·Hub·YOLO·PG·Redis·OSRM·edge·현장 exporter |
| 관리자 | 기존 GCP 테스트 호스트와 공존 유지 | 기존 디스크·DB 보존 | 기존 Admin/admin-web·control DB·감사·Prometheus·Grafana 유지, NCP에 관리자 추가 없음 |
| 학습 | 독립 NCP 한국 s4-g3 4/16 CPU 프로필만 준비 | root20GB / 보존 data100GB | `map-deploy-learning`, `/srv/map-learning`, 합성/승인 산출물만. 현재 VM 미생성·HOLD |

새 enrollment schema2는 `topology=gcp-test-admin-ncp-prod`와 `gcp_cohost_review_sha256`를 필수로 한다. test/admin은 관측한 GCP machine-id와 instance-id가 같아야 하며, 검토한 기존 배포계정·물리볼륨의 공유도 이 두 역할에만 허용한다. secret scope는 공유할 수 없다. prod와 다른 역할의 machine-id·instance-id·계정·볼륨·secret scope 중복은 모두 차단한다. learning은 완전한 reserved identity만 허용한다. 새 install/cache/secret 주입은 schema2 prod만 가능하다. schema1은 과거 증거 검증·복귀용이며 새 역할 설치를 허용하지 않는다. 학습을 동일 serving VM의 Compose 프로젝트로 대체하지 않는다. 실제 독립 VM 네 대가 가동했다는 증거는 아직 없다. 배포 계정은 nologin/독립 그룹이며 Docker 그룹·sudo·SSH 키를 자동 부여하지 않는다. root 소유 receiver의 제한된 ingress/권한 설치는 그 소유자의 별도 작업이다. 학습 worker에는 Docker socket, serving DB/Redis·위치 master key·production SSH가 전달되지 않는다.

Ubuntu 24.04 LTS KVM amd64와 CB2를 선정한다. 정확한 NCP image product code·존·계정 capacity는 아직 입력이다. Docker 공식 Ubuntu 서명 저장소의 Engine/CLI/containerd/buildx/Compose **다섯 버전을 모두 고정**하고 공식 공개키의 SHA256도 enrollment에 묶는다. 예제 버전·mutable latest를 검증 운영 표준으로 만들지 않는다. [NCP Ubuntu24](https://guide.ncloud-docs.com/docs/ubuntu24-kernel-update), [Docker 설치 명세](https://docs.docker.com/engine/install/ubuntu/).

NCP KVM g3는 공급자 디스크 암호화를 지원한다고 가정할 수 없다. 도구는 역할별 **LUKS2 + ext4 추가 디스크**를 요구하며 UUID·실제 mount·crypt 상태를 검사한다. root에는 OS와 공개 bootstrap 설정만, 비밀·Docker·containerd·백업 staging은 암호화 data mount에 둔다. 암호화키는 채팅/Git/enrollment에 넣지 않는다. 현재 helper는 원격 KMS 자동 unlock·crypttab·키 영구 저장을 만들지 않는다. 재부팅 후 운영자가 비공개 키를 주입해 mount를 복구하기 전 Docker/containerd의 ExecStartPre가 실패하도록 한다. 따라서 무인 재부팅·키 분실 복구·전체 RTO는 별도 인수 gate다. [NCP 스토리지 제한](https://guide.ncloud-docs.com/docs/server-storage-modify-vpc).

## 과거 비용 비교 — 현행 배치 견적 아님

아래는 2026-09-07의 운영+별도관리자 2VM 비교다. 현재는 NCP 운영1VM + 기존 GCP 유지 + 클라우드 간 암호화 연결/egress 비용으로 별도 산정해야 한다. 다음 수치를 현재 요금 또는 신규 생성 승인으로 사용하지 않는다.

720h, Linux 시간제, 할인/크레딧 미반영, KRW. 공통 디스크220GB·IP2·snapshot220GB 한 벌·객체120GB 예시를 포함한다.

| 조합 | 세전 | VAT10% 가정 | 선택 조건 |
|---|---:|---:|---|
| 운영2/8 + 관리2/4 | 198,480원 | 218,328원 | 우선 검증 후보 |
| 운영2/8 + 관리2/8 | 215,040원 | 236,544원 | 관리자 메모리 병목이면 관리자만 증설 |
| 운영4/16 + 관리2/8 | 302,880원 | 333,168원 | 운영 CPU/RAM 병목 실측 후 |

단가는 s2=115원/h, c2=92원/h, s4=237원/h, CB2=0.16원/GB/h, snapshot=0.08원/GB/h, IP=5.6원/h, 객체=28원/GB-month이다. 명시 월요금과 720h 시간제 계산은 다르다. 744h 절약안은 세전204,984원/VAT가정225,482.4원이다. **공개요금 근거이며 로그인 계정의 확정 견적은 아니다.** [NCP 공식 요금표](https://www.ncloud.com/charge/price/ko), [Block Storage](https://www.ncloud.com/api-cms/service-product/static/blockStorage).

기존 GCP·학습·외부 API·인터넷 egress·추가 snapshot·객체 요청/전송·NAT/VPN/LB/DNS/알림은 위 합계 밖이다. Object Storage→VPC 사설 다운로드도20원/GB이므로100GB 복원1회2,000원 예시가 더해진다. 같은존 private 통신과 객체 download 과금을 혼동하지 않는다. [공식 네트워크 가격](https://m.ncloud.com/charge/price/ko).

CB2의 100GB/50GB는 XEN SSD의 고정4,000IOPS가 아니다. g3는10–30GB100IOPS에서 용량별 증가/burst 조건을 적용한다. CB2 첫 snapshot은 전체용량, 후속은 실제 사용량에 따라 달라진다. cold/warm OSRM, 동시1→2→4→8, PG·Redis 백업·KMA 저장 동시 작업, 관리자 대시보드·WAL replay/compaction에서 OOM/restart·응답 지연·RPO를 측정해야 용량을 수용할 수 있다. 기존 GCP 순간 메모리 여유를 출시 처리량으로 외삽하지 않는다. [NCP 사양](https://guide.ncloud-docs.com/docs/server-spec-vpc).

확장은 같은 역할의 백업 확인→정지→계정에서 허용된 c2→s2/s2→s4 변경→identity/용량/부팅/서비스 검증 순서다. 옵션이 없으면 같은 역할의 빈 대체 VM으로 복원한다. data 확장은 공급자 확장과 LUKS mapping/filesystem 확장이 모두 필요하며 축소·DB downgrade는 도구에 없다. 이 설치 도구는 **선정 프로필 불변 enrollment**이므로 크기 변경 후에는 기존 enrollment를 임의 편집해 우회하지 않고 별도 재검토/대체호스트 계약을 사용한다. [확장 명세](https://guide.ncloud-docs.com/docs/server-manage-vpc).

학습 CPU40h 비교는 NCP100GB STOP 예시30,792원/월 세전, GCP Iowa e2-standard-4/100GiB 예시$20.36이며 객체·전송 추가다. 미국 배치는 승인 비개인정보/합성 dataset만 검토한다. NCP STOP은1회90일/12개월누적180일 한도가 있어 연중 STOP worker로 단순 운영하지 않는다. root20GB 임시/보존data100GB/객체100GB를 분리한 작업별 시나리오는29,912원 세전 예시다. GPU L4는 정지 할인도 없어 현재 선정하지 않는다. 국내 관리·학습과 GCP 미국 대안의 비용/위치/복구 비교 원문은 세션 evidence `cost-research.md/json`이다.

## 입력과 명령 순서

오프라인 사양과 미입력 목록은 시스템 변경 없이 출력한다.

```sh
python3 scripts/ncp-bootstrap-host.py plan --profile prod-small
python3 scripts/ncp-bootstrap-host.py plan --profile admin-small
python3 scripts/ncp-bootstrap-host.py plan --profile learning-cpu
```

`deploy/ncp-bootstrap/enrollment.template.json`의 placeholder를 공급자 실제 조회값으로 채워 root 전용0600 파일로 보관한다. `approval=pending` 상태로는 설치할 수 없다. 사용자에게 새 호스트/견적 결과를 확인받은 뒤 `approved-empty-host-only`로 만든 **canonical enrollment SHA256**을 별도 검토 경로에서 전달한다. CLI가 내놓은 hash 자체는 사용자 승인이나 공급자 계정 검증이 아니다. quote/network SHA는 외부 검토 문서와 연결해야 하며 값만64자리 채웠다고 검토가 완료되는 것은 아니다.

기존 GCP admin은 reserved로 꾸미지 않고 test와 동일한 실제 관측 host를 기록한다. 미생성 learning 역할만 machine/instance/volume을 모두 `reserved-ROLE-machine`, `reserved-ROLE-instance`, `reserved-ROLE-volume`으로 명시한다. 이는 실제 VM 관측값이 아닌 경계 예약이다. 설치 대상과 기존 test는 예약할 수 없다. 이 구분으로 학습 VM 생성이나 HOLD 해제 없이 운영 빈 호스트를 준비할 수 있다. 다른 역할이 생성되면 다음 호스트의 새 enrollment에 실제 조회값을 기록하며, 이미 승인된 enrollment를 임의 덮어쓰지 않는다.

필요 OS 선행 패키지는 공식 Ubuntu의 python3·ca-certificates·cryptsetup·e2fsprogs·util-linux·systemd다. 배포용 개인 key는 설치하지 않는다. 볼륨 helper는 승인된 **새 추가 디스크**의 serial·용량·rootdisk 분리·미마운트·signature·전체0bytes를 검사한 뒤에만 LUKS2/ext4를 만든다. 알려진 기존 디스크에 format을 적용하거나 실패후 다시 format해 복구하지 않는다. 실제 NCP block 작업과 유료생성은 이 준비 세션에서 수행하지 않는다. 원격 CI에서 자기 생성 sparse 파일에 LUKS/ext4를 만드는 검사는 별도 범위다.

`volume.template.json`도 미입력 상태로 실행되지 않는 템플릿이다. 승인된 호스트에서 `lsblk --json --bytes --output PATH,TYPE,SIZE,SERIAL,MAJ:MIN,FSTYPE,MOUNTPOINTS`로 관측한 정확 serial/bytes/장치 번호를 기록한다. NCP 실제 장치가 serial을 제공하지 않으면 검사를 우회하지 않고 공급자 식별 계약을 추가 검토한다. 새 filesystem UUID를 enrollment에, 다른 새 LUKS UUID를 volume contract에 미리 고정한다. `root_enrollment_sha256`은 host `enrollment-hash` 결과와 같다.

```sh
# 승인된 신규 추가 디스크에서만 실행. 먼저 inspect 결과와 계약을 별도로 검토한다.
sudo python3 scripts/ncp-bootstrap-volume.py inspect \
  --enrollment /root/prod-enrollment.json --device-contract /root/prod-volume.json
python3 scripts/ncp-bootstrap-volume.py contract-hash \
  --enrollment /root/prod-enrollment.json --device-contract /root/prod-volume.json
sudo python3 scripts/ncp-bootstrap-volume.py prepare \
  --enrollment /root/prod-enrollment.json --device-contract /root/prod-volume.json \
  --key-file /run/private-volume.key --format-empty --approved-volume-sha256 REVIEWED_VOLUME_SHA256
# 재부팅 또는 안전한 close 후: format 없이 기존 UUID/journal 대조 후 다시 연다.
sudo python3 scripts/ncp-bootstrap-volume.py restore-mount \
  --enrollment /root/prod-enrollment.json --device-contract /root/prod-volume.json \
  --key-file /run/private-volume.key
```

키 파일은 root 소유0600/32–4096bytes이며 별도 안전한 복구 사본이 필요하다. 루트 디스크/기존 서명/중단된 format은 자동 재포맷되지 않는다. 볼륨 close는 먼저 host rollback으로 runtime을 정지시킨 뒤 volume `rollback`을 사용하며 장치 내용을 삭제하지 않는다.

```sh
# 아래는 사용자 승인 뒤 빈 대상 호스트에서 수행할 절차다.
python3 scripts/ncp-bootstrap-host.py enrollment-hash --manifest /root/prod-enrollment.json
sudo python3 scripts/ncp-bootstrap-host.py preflight --manifest /root/prod-enrollment.json
sudo python3 scripts/ncp-bootstrap-host.py install --manifest /root/prod-enrollment.json \
  --docker-key /root/reviewed-docker.asc --approved-enrollment-sha256 REVIEWED_CANONICAL_SHA256
sudo python3 scripts/ncp-bootstrap-host.py verify --manifest /root/prod-enrollment.json
# 값은 argv/.env/로그에 넣지 않는다. 같은 값 재주입은 멱등, 다른 값은 교체하지 않는다.
sudo python3 scripts/ncp-bootstrap-host.py secret --manifest /root/prod-enrollment.json \
  --key JWT_SECRET < /root/private-jwt-input
```

설치는 프로필 CPU/RAM/disk 하한, machine-id/hostname, 암호화 mount UUID, 기존 Docker/컨테이너 자료와 systemd override 부재를 검사한다. 초기 apt 중 서비스 자동기동을 막고, Docker/containerd를 mask한 상태에서 설정을 완성한 뒤 시작한다. data-root는 역할 mount, socket group은root다. 기존 파일을 덮어쓰지 않고 temp+fsync+원자 게시와 영속 transaction/lock을 사용한다. 실패 시 기존 GCP로 fallback하지 않는다.

## 이미지·지도·receiver 인수

`ncp-bootstrap-artifacts.py verify`는 offline 검증, `stage`는 외부에서 검토한 `security-approval.json` SHA256을 필수로 받는 파일 설치다. contract가 이미지별 정확 source commit/platform/digest, 역할 Compose 검토본, 지도·Caddy·receiver 전체 checksum을 묶는다. 후보 상태를 보존하며 candidate는 stage할 수 없다. `serving_approval=false`가 필수다. Admin/learning은 기존 `verify-role-manifest.py` 계약을 재사용하고 role boundary/HOLD/권한·mount 제약을 검사한다. 공개 검토본에는 실제 secret 대신 `SECRET_REF_*`만 둔다.

운영 edge는 registry에 없을 수 있는 검증된 **Caddy 전달 archive**의 OCI index/platform/config digest 체인을 쓴다. `docker/caddy-security/install-artifact-20260906.json`의 파일 SHA256 `e6c361045e7a57f94aa08b35342972530c4f09a699ab86e6f76999609a82a2cb`가 신뢰 anchor다. archive SHA256 `c85c7192e92644fccd75d86bdfca599425603a647e77183b29effaddfe697fcc`/68,931,584bytes, config ID `sha256:751250cd5a230dc11a3d62ebcfa8c094fca3621705526360d772ea26581ada57`를 원본 installer로 검증한다. 태그를 새로 pull해 대체하지 않는다. SHA가 바뀌면 root의 새 검토가 필요하다.

OSRM은 기존 `osrm-release.py`의 engine/profile/40 runtime files·archive checksum·크기·경로 계약을 재사용한다. tar path traversal/symlink/hardlink/device/중복/누락을 거절한다. 운영에서 지도 전처리를 하지 않는다. 기존 전국 지도 원본의 재전처리나 대용량 로컬 복사는 이번 준비 범위가 아니다.

receiver는 파일·source commit·owner·checksum·`empty-host-ncp-v1` capability의 **불투명 전달 계약**으로만 소비한다. prod는root, admin/learning은session-c가 소유한다. GCP 고정 receiver를 이름만 바꿔 NCP 호환으로 승인하지 않는다. capability 문자열만 추가하는 것은 실제 검증의 대체가 아니다. 아직 NCP용 receiver/기반 이미지 보안 승인이 없으면 승인 bundle을 만들지 않고 후보/남은 입력으로 인계한다.

```sh
python3 scripts/ncp-bootstrap-artifacts.py verify --bundle /root/review-bundle --role prod
sudo python3 scripts/ncp-bootstrap-host.py cache --manifest /root/prod-enrollment.json \
  --bundle /root/review-bundle --release-name reviewed-release \
  --security-approval-sha256 REVIEWED_APPROVAL_FILE_SHA256
```

host `cache`는 호스트의 role/account와 검토본 target을 대조하고 원자 stage 후 `docker pull --platform linux/amd64 repository@sha256:...`와 실제 inspect RepoDigests/platform을 대조한다. Caddy는 원본 installer의 이미지 load·binary checksum·격리 HTTP/TLS smoke와 `pull_policy: never` 결과를 쓴다. registry source provenance/취약점 검토는 별도 approval evidence가 담당한다. daemon 이미지 cache가 채워지는 것은 서비스 배포가 아니다. receiver·Compose를 실행하는 기능은 없다.

## 백업·복귀·네트워크

backup helper는 live PG/Redis 디렉터리를 복사하지 않는다. 닫힌 PG 논리 dump/roles 또는 Redis snapshot의 기존 helper manifest, 또는 명시한 stopped synthetic fixture만 받는다. 역할·환경·source 시각·일관성·파일SHA·권한을 검사해 snapshot을 만들고 새 목적지에만 복원한다. 업로드는 age 수신자 암호화 후 NCP 한국 고정HTTPS endpoint·private ACL·UUID prefix에 보관하며 다시 다운로드해 cipher SHA256을 확인한 뒤 transport manifest를 마지막 게시한다. 다운로드에는 별도 신뢰 경로의 transport SHA256과 private age identity가 필요하다. 무결성 checksum을 publisher 인증으로 오해하지 않는다.

`kr.object.ncloudstorage.com`/`kr-standard`의 운영/관리/학습 bucket/prefix와 IAM은 분리한다. 위치·보존기간·정책·키 복구·bucket 실제 설정은 사용자/계정 입력이다. Object Storage 한국은 리전 범위, snapshot은존 범위이며 독립 공급자 DR을 자동 보장하지 않는다. 별도 Ncloud Storage 상품으로 바꿔 호출하지 않는다. [NCP Object Storage API](https://api.ncloud-docs.com/docs/storage-objectstorage).

`backup --help`와 각 subcommand help가 정확한 입력 계약이다. `--offhost-dir`은 별도 local filesystem의 **합성 fixture만** 허용한다. 이 결과를 NCP 원격저장/DB restore/RPO/RTO 완료로 세지 않는다. 실제 DB restore는 새 역할의 검증된 DB tool에서 실행하고 데이터·역할·권한·삭제 이력·키 대조를 별도로 기록한다. host rollback은 활성 컨테이너/명명볼륨이 있으면 거절하고, 비어있는 준비 호스트에서 daemon을 stop/disable/mask한다. root 경계 설정과 private config 사본·계정·패키지·데이터·secret은 보존한다. rollback 중단은 ledger로 재개하며 DB downgrade/삭제/볼륨 제거가 없다.

```sh
sudo python3 scripts/ncp-bootstrap-host.py rollback --manifest /root/prod-enrollment.json
python3 scripts/ncp-bootstrap-backup.py --help
```

ACG/NACL/route 템플릿은 `network.template.json`이다. 실제 CIDR/인증·MTU·DNS/time sync·외부 API·registry·백업 HTTPS 허용 목록은 root review로 고정한다. 운영만 public80/443, SSH는운영자 경로, NCP DB/metrics는loopback+검증된 암호화 management 경로로 제한한다. GCP 중앙 관리자 공개화는 별도 TLS·로그인·역할 검증을 통과한 기존 관리 진입점을 사용한다. NCP의 원시 DB/exporter 포트를 공인망에 열지 않는다. Docker publish가 UFW를 우회할 수 있으므로 host UFW 하나로 격리를 입증하지 않는다. 관리자/학습 장애 시 serving과 현장 백업이 유지돼야 하며 관리자 장애 감지는 별도 경로로 관측해야 한다. [Docker firewall 계약](https://docs.docker.com/engine/install/ubuntu/).

## 검사와 남은 gate

```sh
python3 -B -m unittest discover -s deploy/ncp-bootstrap -p 'test_*.py' -v
```

로컬 테스트는 실제 private 파일 설치·검증·복귀와 합성 archive/backup을 사용하고 OS/package/daemon adapter를 주입한다. 실제 NCP 설치 증거가 아니다. age가 있으면 실제 합성 암복호화도 수행한다. `deploy/ncp-bootstrap/linux_acceptance.py` 및 `ci-workflow.yml`은 별도 원격 Ubuntu24/systemd/Docker fixture용이다. 전용 신규 `.github/workflows/ncp-bootstrap-acceptance.yml` 하나를 별도 범위 확인 뒤 추가했다. 기존 workflow는 수정하지 않았다. 이 작업 branch의 bootstrap 변경만 순차 실행하며 develop/master·NCP/GCP 배포는 하지 않는다. 정확 SHA·run·artifact와 실행여부는 루트 세션 evidence HANDOFF에 기록한다.

GitHub draft release는 push 권한이 있어야 조회되므로 전달 전용 job만 `contents: write` 토큰으로 정확 기존 draft asset을 GET 한다. 게시·업로드 API는 사용하지 않는다. privileged fixture는 별도 `contents: read` job에서 실행하고 GitHub 토큰/runner Docker socket을 guest에 전달하지 않는다. [GitHub draft 조회 계약](https://docs.github.com/en/rest/releases/releases#list-releases).

남은 실제 gate: 계정 로그인 견적·quota·정확image/zone, 생성 사용자확인, LUKS 키주입/재부팅복구, 기반이미지 보안 및 NCP용 receiver 승인, network/CIDR/관리 인증, 신규NCP S3 왕복·DB/전체역할 복원·알림도착, 목표p95/p99/동시수용과 RPO1h/RTO4h 실측이다. 이번 설치 준비를 운영전환·스토어출시 완료로 표시하지 않는다.

현행 데이터 경로: NCP 운영 대상의 관리·관측 정보가 기존 GCP us-central1-a로 이동할 수 있다. 허용 필드·마스킹·보존기간을 기록하고 정책 검토에 반영한다. 국내 NCP 실행만으로 모든 처리의 국내 배치를 의미하지 않는다. NCP 자체 백업·감시는 GCP 관리자 장애에도 지속되어야 한다. GCP6앱 provenance/guard와 NCP4앱 배치를 구분하며 기존 GCP 관리자 분리 스위치를 켜지 않는다.

## NCP 운영 현장 백업 runner

`scripts/ncp-production-backup.py`와 `deploy/map-prod-{pg,redis}-backup.{service,timer}`가 GCP 관리자에 의존하지 않는 운영 백업 실행 경로다. 설치 전 검토한 infra 소스를 root 소유 `/opt/map-service-infra`에 고정하고, 실제 schema2 enrollment·LUKS mount·정확 PG/Redis container/image ID를 확인한다. `deploy/ncp-bootstrap/production-backup.template.json`을 채운 설정은 `/srv/map-prod/secrets/backup.json` root0600에 저장한다. 템플릿 그대로는 실행되지 않는다. 설정의 enrollment SHA는 canonical hash이며, 이후 container가 교체되면 receiver와 동일한 `/srv/map-prod/deploy/deploy.lock`을 소유한 상태에서 정확한 새 ID로 갱신해야 한다.

NCP 정적 Object Storage credential INI는 `/srv/map-prod/secrets/NCP_BACKUP_CREDENTIALS` root0600만 사용한다. PG DB 비밀번호는 호스트 argv나 설정에 전달하지 않으며 기존 컨테이너 인증 경로로 논리 dump를 생성한다. Redis 비밀번호도 기존 컨테이너에서만 사용한다. Object Storage 전송은 검증된 닫힌 helper 산출물 → age 암호화 → private UUID 경로 업로드 → ciphertext/transport 재다운로드 SHA 검증 → 성공 상태 기록 순서다. PG 원본 논리 dump·역할 비밀번호 제외·row count 추출, Redis fork/memory 제한·snapshot 세대·두 격리 복원·TTL 의미 검증은 기존 helper를 재사용한다. Redis의 기본 GCS 백업 동작은 바뀌지 않는다.

타이머는 PG 매시00/30분, Redis15/45분이며 process group 전체에 900초 상한을 둔다. 두 작업은 prod deploy.lock 다음 backup.lock 순서로 잠근다. 지연/실패는 마지막 성공 snapshot 시각을 보존하며 새 성공으로 표시하지 않는다. 상태는 `/srv/map-prod/deploy/{pg,redis}-backup-status.json`이다. 검증된 host·project·container·image 일치 전에 DB에 접근하지 않는다. GCP 주소·API·credential·상태는 조회하지 않는다.

백업 생성물과 실패한 새 작업 디렉터리는 `/srv/map-prod/backups`에 보존한다. 이 runner는 기존 사본·데이터를 삭제하지 않는다. 사본 수명과 용량 모니터링은 별도 운영 gate이며, PG는 관측 DB 크기8배+2GiB 여유를 먼저 요구하고 Redis/전송 helper도 기존 용량 제한을 적용한다. 실제 NCP에서 key pair 복구, 두 번 이상의 timer/S3 암복호화 왕복, 새로운 DB restore·애플리케이션/권한/키·RPO/RTO 및 알림 수신은 여전히 별도 인수 검사다. 단위검사의 mock transport 성공을 실제 NCP 암복호화·전체 서비스 복원으로 기록하지 않는다.
