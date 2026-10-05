# GCP 운영 백업 — GCS 암호화 전송

운영 VM `map-prod` 의 PostgreSQL·Redis·관리 control DB 를 30분마다 닫힌 백업으로 만들고, age X25519 로 암호화해 `gs://map-prod-backups` 에 올린다. NCP 실행기(`ncp-production-backup.py`)의 순서와 검사 함수를 그대로 재사용하고, NCP 원본 스크립트는 고치지 않는다.

- `scripts/gcs-production-backup.py <pg|redis|admin>`: 실행기. `deploy.lock` → `backup.lock`(수신기와 같은 순서) → 호스트 등록 검사 → source 핀 대조 → pending guard → 수집·닫힌 계약·snapshot → GCS 업로드 → 자체 영수증 → 평문 삭제 → 상태 파일·결과 줄.
- `scripts/gcs_backup_transport.py upload|download`: 전송. 업로드는 create-only(`--if-generation-match=0`) 뒤 다시 내려받아 SHA256 을 대조하고, `transport.json` 을 마지막에 올린다. 병렬 합성 업로드는 끈다(조각 삭제 권한이 없다).

## 설치 전제

1. `/opt/map-ops-gcp` 는 이 저장소의 **전체 체크아웃**이다. `scripts/` 만 두면 안 된다. 등록 검사가 `deploy/ncp-bootstrap/profiles.json` 을 읽기 때문이다. 소유자는 root:root 이고 그룹·기타 쓰기가 없어야 한다(`chown -R root:root`, `chmod -R go-w`, 조상 디렉터리 포함). 실행기는 import 전에 `scripts` 경로와 import 대상 8개 파일이 root 소유·go-w·단일 링크인지 확인하고, 아니면 FAILED 로 끝난다. 유닛은 `-X pycache_prefix=/run/<유닛>` 으로 바이트코드를 실행마다 비어 있는 런타임 디렉터리에서만 찾으므로, 체크아웃 안의 `__pycache__` 는 읽지 않는다(`-B` 는 쓰기만 막고 이미 있는 `.pyc` 는 읽는다).
2. apt 판 `google-cloud-cli`(`/usr/bin/gcloud`)와 `age` 를 설치한다. snap 판 gcloud 는 거부한다. NoNewPrivileges·ProtectHome 샌드박스에서 실패하기 때문이다. `/usr/bin/gcloud` 가 없거나, 실제 대상이 일반 파일이 아니거나, snap(`/snap/…` 아래 또는 `/usr/bin/snap`)을 가리키면 `apt_gcloud_required` 로 FAILED 다.
3. VM 은 접근 범위 cloud-platform 과 VM SA `map-prod-vm@` 으로 인증한다. 이 SA 는 `map-prod-backups` 에 대해 버킷 단위 objectCreator+objectViewer 만 가진다(삭제 권한 없음).
4. `/etc/map-ops-gcp/backup-gcs.json` 을 root 0600 으로 만든다. `deploy/gcp/backup-gcs.template.json` 의 모든 `REQUIRED_*` 를 **설치 때 실측한 값**으로 채운다. 자리표시자가 남아 있으면 거부된다. NCP 의 `backup.json` 이나 NCP 자격은 읽지 않는다.
   - `enrollment_sha256`: 등록 파일의 정규 JSON SHA256.
     `python3 -c 'import hashlib,json;v=json.load(open("/var/lib/map-bootstrap/enrollment.json"));print(hashlib.sha256(json.dumps(v,sort_keys=True,separators=(",",":")).encode()).hexdigest())'`
   - `container_id`·`image_id`: `docker inspect --format '{{.Id}} {{.Image}}' <컨테이너>`. 대상은 compose project `map-prod` 의 `postgres`·`redis` 와 `map-admin-prod` 의 `admin-control-db` 다. `postgres.database` 는 `map_prod` 로 고정이다.
   - `age_recipient`: 보관(escrow)한 age 신원의 공개 수신자(`age1…`)다. 개인 키(신원)는 VM 에 두지 않는다.
   - `admin` 블록은 admin 스택을 설치하고 그 핀을 실측한 뒤에만 넣는다. 그 전에는 블록을 지운다.
5. `/var/lib/map-bootstrap/enrollment.json`(root 0600)이 있어야 하고, 수정하지 않은 `ncp-bootstrap-host.py` 의 validate·verify 를 매 실행 통과해야 한다. 이 검사는 NCP 위상 라벨을 그대로 요구한다. GCP 의 실제 배치(admin 이 map-prod 와 같은 VM)를 적으면 거절된다. 작성 절차는 [NCP 빈 호스트 설치 계약](NCP_HOST_PROFILES.md)의 '입력과 명령 순서'다.
   - 라벨: schema_version 2, topology `gcp-test-admin-ncp-prod`, role `prod`, profile 은 `deploy/ncp-bootstrap/profiles.json` 의 prod 프로필(`prod-small`·`prod-headroom`), data_encryption `luks2`, learning_hold true.
   - inventory(test·prod·admin·learning 넷): `test`·`admin` 의 provider 는 `gcp`, `prod` 는 `ncp` 다. admin 은 옛 GCP 시험 호스트와 같은 machine_id·instance_id 를 쓰는 레거시 표기를 유지한다. admin 을 prod 와 같은 머신으로 적으면 `cross-role identity, account, volume or secret scope reuse` 로 거절된다. machine_id·instance_id·deploy_account·data_volume_id 는 test 와 admin 사이에서만 공유할 수 있고, secret_scope 는 어느 두 역할도 공유할 수 없다. learning 은 `reserved-learning-machine`·`reserved-learning-instance`·`reserved-learning-volume` 이다. prod 항목은 enrollment 최상위 machine_id·instance_id 와 같고, deploy_account `map-deploy-prod`, secret_scope `map-prod` 다.
   - 호스트: root 로 도는 Ubuntu 24.04 amd64, hostname 과 `/etc/machine-id` 가 enrollment 값과 같다. `/srv/map-prod` 는 enrollment data_uuid 의 ext4·rw 마운트이고 원본은 LUKS2 `/dev/mapper/…` 다. `/srv/map-prod`·`/srv`·`/etc`·`/var/lib` 는 root 소유이고 그룹·기타 쓰기가 없다.
   - 용량 하한(vCPU 는 프로필 값 그대로, 메모리·디스크는 프로필 값의 90%, 1GB=10⁹ 바이트): `prod-small` 은 vCPU 2·메모리 7.2GB·root 파일시스템 36GB·data 파일시스템 90GB 이상, `prod-headroom` 은 vCPU 4·메모리 14.4GB 이상(디스크는 같다).
   - 설치 상태: `transaction.json` status `installed`, 관리 파일(daemon.json·systemd drop-in·verify-mount·apt 저장소와 키·containerd.toml)의 SHA256·권한 그대로. Docker 다섯 패키지(docker-ce·docker-ce-cli·containerd.io·docker-buildx-plugin·docker-compose-plugin)가 enrollment 에 고정한 판 그대로다(docker-ce 와 docker-ce-cli 는 같은 판). `docker info` 의 DockerRootDir 는 `/srv/map-prod/docker`, `/var/run/docker.sock` 은 root:root 0660 이다. 계정 `map-deploy-prod` 는 홈 `/var/lib/map-deploy-prod`, 셸 `/usr/sbin/nologin`, 자기 그룹 말고는 그룹이 없다.

   위 검사 중 하나라도 어긋나면 모든 실행이 FAILED 다. 이 검사들의 stderr 사유는 `operation_failed` 로만 나온다. 어느 검사인지는 root 로 `python3 -B /opt/map-ops-gcp/scripts/ncp-bootstrap-host.py verify --manifest /var/lib/map-bootstrap/enrollment.json` 를 실행해 `reason` 으로 확인한다.
6. 유닛 6개를 `/etc/systemd/system/` 에 root 0644 로 복사한다. `systemd-analyze verify /etc/systemd/system/map-prod-*-backup-gcs.{service,timer}` 가 깨끗해야 한다. 그다음 `systemctl daemon-reload` 를 실행한다.

## 유닛과 타이머

| 유닛 | 일정(Persistent, AccuracySec=1s) | 대상 |
|---|---|---|
| `map-prod-pg-backup-gcs.{service,timer}` | 매시 00·30분 | `map-prod`/`postgres` DB `map_prod` |
| `map-prod-redis-backup-gcs.{service,timer}` | 매시 15·45분 | `map-prod`/`redis` RDB |
| `map-prod-admin-backup-gcs.{service,timer}` | 매시 05·35분 | `map-admin-prod`/`admin-control-db` |

- 서비스는 모두 oneshot·root 로 돈다. 하드닝은 NCP 유닛과 같다(ProtectSystem=strict, ProtectHome, NoNewPrivileges, PrivateTmp, 쓰기는 `/srv/map-prod` 와 docker 소켓만, 900초 상한).
- gcloud 의 HOME·CLOUDSDK_CONFIG 는 유닛별 `/run/<유닛>`(0700)이다. 이 디렉터리는 실행이 끝나면 사라진다.
- 켜기: `systemctl enable --now map-prod-pg-backup-gcs.timer map-prod-redis-backup-gcs.timer`.
- **admin 타이머는 admin 스택 설치와 admin 핀 기록 뒤에만 켠다.** 핀 없이 admin 유닛이 돌면 건너뛰지 않고 FAILED 로 끝난다(경보가 울린다).

## 결과 줄과 로그 지표

stdout 의 줄은 저널 MESSAGE 가 된다. Ops Agent 수신기 `map_journald` 는 유닛 이름이 `^map-prod-.*backup.*\.service$` 인 줄만 남긴다. 여기에는 PID1 이 `UNIT=` 필드로 남기는 'Failed with result' 줄도 들어간다.

| 상황 | stdout | 종료 |
|---|---|---|
| 성공 | 결과 JSON 한 줄(`code, success, rpo_1h_overdue, remote, transport_sha256`, 비밀 없음) + `MAP_BACKUP_RESULT=COMPLETE kind=<pg\|redis\|admin>` | 0 |
| 잠금 점유, 1시간 안에 성공 있음 | 결과 JSON 만, 결과 줄 없음 | 0 |
| 잠금 점유가 1시간 초과, 그 밖의 모든 실패 | `MAP_BACKUP_RESULT=FAILED` (사유 코드는 stderr JSON) | 1 |

- 성공 지표는 `MESSAGE=~"^MAP_BACKUP_RESULT=COMPLETE"` 이고, 75분 동안 없으면 경보한다.
- 실패 지표는 `MESSAGE=~"^MAP_BACKUP_RESULT=FAILED"` 또는 (`UNIT` 일치 AND 'Failed with result')이다. 뒤쪽은 시간 초과·강제 종료처럼 결과 줄이 없는 실패를 잡는다.
- 상태 파일은 `/srv/map-prod/deploy/{pg,redis,admin}-backup-gcs-status.json` 이다. 손상된 이전 상태 파일은 무시하고 새로 쓴다.
- 영수증은 `/srv/map-prod/deploy/backup-receipts-gcs/` 에 종류별 최근 96개를 둔다. 실패한 작업은 종류별 `/srv/map-prod/backups-gcs/<pg|redis|admin>/` 에 검토용으로 남는다. 한 종류에 4개가 쌓이면 그 종류만 `backup_failed_jobs_require_review` 로 멈추고, 다른 종류는 계속 돈다.

## 원격 배치와 수명

- 객체는 `gs://map-prod-backups/prod/<kind>/<uuid32>/{backup.age,transport.json}` 에 둔다. 같은 이름을 덮어쓰지 않는다.
- 복원의 신뢰 기준은 결과 JSON 의 `remote`·`transport_sha256` 이다. 저널(Cloud Logging)에 사본이 남는다.
- 수명은 버킷 전체 규칙 Delete age **7일**이다(`gcp/terraform/envs/prod/locals.tf` 의 `backup_retention_days = 7`). 이 값은 `RETENTION_DAYS` 및 고지문의 7일과 같으며, `tests/test_gcs_backup_transport.py` 가 셋을 묶는다. 이 도구에는 삭제 기능이 없다.

## 첫 1회 실제 SA 왕복(인수 시험)

1. `systemctl start map-prod-pg-backup-gcs.service`
   `journalctl -u map-prod-pg-backup-gcs -n 5` 로 `COMPLETE kind=pg` 와 `remote`·`transport_sha256` 을 확인한다.
2. LUKS 볼륨 위에 디렉터리를 0700 으로 만든다.
   `install -d -m 0700 /srv/map-prod/restore-keys /srv/map-prod/restore-check /run/map-restore`
   보관한 age 신원을 `/srv/map-prod/restore-keys/identity.txt` 에 0600 으로 꺼낸다.
3. 내려받는다. 신원이 자기 검사(0600 파일·0700 디렉터리·LUKS2)를 통과하면, 그 뒤로는 성공이든 실패든(입력 오류 포함) 끝날 때 shred 로 지워진다. 다시 시도하려면 보관본에서 다시 꺼낸다. 신원 검사에서 거절되면 파일이 그대로 남으므로 직접 지운다. 기본 신선도 한도는 보존 기간(7일)이며, 더 짧게 하려면 `--max-age-seconds` 를 준다.
   `env HOME=/run/map-restore CLOUDSDK_CONFIG=/run/map-restore python3 -B /opt/map-ops-gcp/scripts/gcs_backup_transport.py download --remote <remote> --transport-sha256 <sha> --target /srv/map-prod/restore-check/pg-1 --age-identity-file /srv/map-prod/restore-keys/identity.txt`
4. 격리 복원 검사를 한다. 이미지는 운영 postgres 와 같은 **고정 digest** 로 지정한다. 가변 태그를 받아오지 않게 하기 위해서다.
   `POSTGRES_IMAGE="$(docker image inspect --format '{{index .RepoDigests 0}}' <postgres.image_id>)" python3 -B /opt/map-ops-gcp/scripts/pg_backup.py restore --prod /srv/map-prod/restore-check/pg-1/postgres.helper.json`
   `"restore": "complete"` 와 표 행 수 대조를 확인한다. 이 검사는 1GiB 컨테이너를 잠시 띄운다. 확인한 뒤 평문 복원본을 지운다.
5. redis 도 1번을 같은 방식으로 실행해 COMPLETE 를 확인한다.

## admin control DB 의 한계

`pg_backup.py restore` 는 `hub_data`·`user_service` 표를 요구하므로 admin 덤프(`admin_data`)는 검증하지 못한다. admin 백업은 닫힌 계약 verify 까지만 보증한다(덤프 2개의 순서·크기·SHA256, helper·snapshot 일치, 업로드 재다운로드 대조). 실제 DB 복원 검사는 별도 절차가 필요하다.

## 컨테이너가 바뀌면

postgres·redis·admin-control-db 컨테이너를 다시 만들면 `container_id` 가 바뀐다. 이미지를 바꾸면 `image_id` 가 바뀐다. 설정을 고칠 때까지 모든 실행이 `production_source_drift` 로 FAILED 다. 새 값을 실측해 `backup-gcs.json`(root 0600)을 고치고, 다음 실행에서 COMPLETE 를 확인한다. 등록 파일이 바뀌면 `enrollment_sha256` 도 다시 계산한다.
