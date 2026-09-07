# Redis 외부 백업과 격리 복원 — 2026-09-06 실행 원장

## 변경 이유와 범위

기존 PostgreSQL 외부 백업 성공은 Redis 복원을 증명하지 않는다. `redis_backup.py`는 현재 실행 중인 Redis의 일관된 RDB를 저장하고 기존 GCS 자격증명을 재사용해 별도 `.../test/redis-v1`에 보관한다. 새 VM/키/버킷, Redis 버전 변경, serving 볼륨 교체, 사용자 키·값 조회/출력, 학습 capture/export 활성화는 없다. 과거 백업 자동 삭제도 없다.

Redis는 BGSAVE에서 fork하고, 완성한 RDB를 원자적으로 rename하므로 복사 중 serving을 정지할 필요가 없다. [공식 persistence 설명](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)과 [BGSAVE](https://redis.io/docs/latest/commands/bgsave/), [LASTSAVE](https://redis.io/docs/latest/commands/lastsave/)에 따라 기존 저장·AOF rewrite가 없을 때만 시작하고, 새 LASTSAVE 및 정상 종료를 확인한다. LASTSAVE의 초 정밀도로 과거 파일을 잘못 선택하지 않도록 서버의 다음 초까지 기다린다. 복사본은 최소한 이 확인한 snapshot만큼 신선하며 자동 저장이 이어지면 더 최신 RDB일 수 있다.

## 백업 계약

1. `--test`와 `--prod` 중 하나를 명시하고 해당 Compose project/service와 실행 중인 Redis 이미지 ID를 검증한다. 목적지는 환경명과 `/redis-v1`을 포함해야 한다.
2. 실제 used_memory/RSS, container memory limit, host MemAvailable, 디스크 여유를 측정한다. 진행 중 BGSAVE/AOF rewrite, AOF 오류, fork 여유 부족이면 시작하지 않는다. 현재 검토한 소규모 운영 범위는 used_memory 16MiB 이하이며 그보다 커지면 복원 자원 상한 재검토를 요구한다. 기존 환경 설정을 임의 증설하지 않는다.
3. BGSAVE 완료 후 `/data`의 완성된 RDB를 0700 디렉터리/0600 파일에 복사한다. 새 UTC 마이크로초·UUID 이름을 쓰며 기존 산출물을 덮어쓰지 않는다.
4. 동일 이미지 ID/플랫폼을 `--pull never`로 실행한다. 새 복원 컨테이너는 network none, read-only, CPU0.5, 메모리/스왑 합계128MiB, `/data`64MiB tmpfs와 `/tmp`16MiB tmpfs뿐이다. source volume/host bind/socket/공개 포트는 없다. 실제 `redis-check-rdb` 및 로딩이 성공해야 한다.
5. RDB 안의 TTL은 절대 시각이다. [Redis EXPIRE 설명](https://redis.io/docs/latest/commands/expire/)과 [7.4 RDB 로딩 소스](https://github.com/redis/redis/blob/7.4/src/rdb.c)에 따라 복원 시 이미 만료된 키는 primary에서 사라진다. 따라서 첫 격리 컨테이너는 network-none의 연결되지 않은 loopback replica로 열어 DB별 RDB 키 수를 재현한다. 두 번째는 일반 primary로 열어 비만료 키 수가 정확히 보존되고 TTL 키만 감소하는지 확인한다. replica는 실제 서비스에 연결하지 않는다. 원래 설정한 database 개수도 보존한다.
6. 기존 `pg_backup.upload`의 GCS 다운로드 SHA256 검증을 재사용한다. RDB의 원격 바이트가 일치해야 manifest를 마지막으로 전송하고 그 manifest도 원격 checksum을 검사한다. 실패하면 성공 상태를 기록하지 않으며 다음 실행은 새 이름으로 재시도한다. 로컬 파일과 원격 부분 산출물을 자동 삭제하지 않는다.

manifest에는 환경, UTC snapshot 시각, 정확한 이미지 ID/플랫폼/Redis 버전, DB 개수, DB별 집계, RDB 파일 크기/SHA256, fork/COW/소요 시간 및 복원 결과가 있다. 키·값·자격증명·사용자 원문은 없다. 실제 RDB에는 서비스 데이터가 있으므로 기존 backup IAM와 private 파일 권한을 유지한다.

## Timer, lock와 실패 회복

- 기존 PG: 매시 00/30분. 새 Redis: 15/45분. 각 timer에 최대30초 지연이 있다.
- 두 timer의 entry point는 `backup_job.py`를 통해 `deploy.lock` 다음 `backup.lock`을 동일 순서로 nonblocking flock한다. 배포 receiver는 이미 deploy.lock 안에서 PG 직접 백업을 실행하므로 이 wrapper를 다시 호출하지 않는다.
- lock 충돌은 `LOCK_BUSY` deferred로 기록하고 이번 실행을 건너뛴다. 배포와 다른 백업을 동시에 시작하지 않는다. 다음 30분 실행이 다시 시도한다.
- 540초 child timeout 때 그 작업의 process group에 SIGTERM, 10초 뒤 필요하면 SIGKILL을 보낸다. systemd의 600초 control-group 상한도 있다. Redis helper의 SIGTERM 처리와 `finally`는 자신이 만든 UUID 복원 컨테이너만 종료/제거한다. 강제 프로세스 종료 시 남은 fixture는 이름/label을 확인한 운영자가 정리하며 기존 serving 컨테이너는 건드리지 않는다.
- PG `backup-status.json`, Redis `redis-backup-status.json`은 원자적으로 갱신한다. 실패·deferred도 시각/코드를 남기고 마지막 성공 및 Redis snapshot 시각을 보존한다. 손상 상태 파일이 후속 성공을 영구 차단하지 않는다. `rpo_1h_overdue`는 Redis snapshot 기준으로 평가한다. 이 값은 작업 실행 당시의 평가이며 외부 모니터는 현재 시각과 snapshot_at을 비교해야 한다.
- 주기 설정은 RPO 달성을 보장하지 않는다. 외부 알림 도착, 여러 연속 실행, 원격에서 내려받은 artifact 복원, 역할 전체 RTO는 별도 실제 증거가 필요하다.

## GCP 적용과 복귀 준비

R2 배포 종료 후 root가 동일 배포 lock 아래 적용한다. 먼저 기존 PG timer 상태를 기록하고 timer만 잠시 멈춘다. 이미 실행 중인 PG backup service는 종료시키지 않고 끝날 때까지 기다린다. 기존 `/usr/local/lib/map-deploy/backup-test-service.py`, pg_backup.py 및 unit 파일을 private 버전 디렉터리에 보존한다. 새 helper의 의존성인 pg_backup.py가 검토한 소스와 같은지 SHA256을 대조한다.

함께 설치할 파일:

- `scripts/backup_job.py`, `scripts/redis_backup.py`, `scripts/backup-test-redis-service.py`
- 수정한 `scripts/backup-test-service.py` (PG도 같은 lock에 참여해야 하므로 함께 적용)
- `deploy/map-test-redis-backup.service`, `deploy/map-test-redis-backup.timer`

기존 `/etc/map-deploy/backup.env`와 GCP 자격증명 파일은 그대로 사용한다. Redis local directory는 기존 BACKUP_DIR 아래 `/redis-v1`, 원격은 BACKUP_REMOTE 아래 `/redis-v1`이다. 원문 env/key를 출력하거나 shell source하지 않는다. systemd daemon-reload 뒤 기존 PG timer를 원상 활성화하고 새 Redis timer를 활성화한다. lock을 놓은 뒤 첫 Redis backup service를 수동1회 시작하여 실제 snapshot/원격checksum/복원/status를 확인한다. PG와 Redis의 timer/listeners 및 다음 실행 시각을 확인한다. 이 문서 작성 시 GCP mutation은 아직 실행하지 않았다.

복귀는 새 Redis timer를 비활성화하고, 실행 중인 작업은 완료를 기다리거나 해당 service의 정상 SIGTERM 종료를 요청한다. 보존한 PG entry point/unit을 되돌려 PG timer를 원래 상태로 복구한다. 생성된 backup 파일·원격 object·serving DB/Redis volume은 삭제하지 않는다.

원격 복원 시험은 새 private 디렉터리에 manifest와 참조한 RDB를 받아 checksum을 확인하고 다음 명령으로 실행한다. source image ID가 없으면 먼저 검토된 동일 이미지 산출물을 준비해야 하며 mutable 태그를 임의 pull하지 않는다.

```sh
python3 /usr/local/lib/map-deploy/redis_backup.py restore-check --test \
  --manifest /private-new-directory/map-redis-test-INSTANCE.manifest.json
```

실제 운영 복구에서는 기존 AOF를 둔 상태로 RDB만 덮어쓰면 AOF가 우선할 수 있다. 새 격리 볼륨에서 RDB를 `appendonly no`로 읽고 검증한 뒤, 승인한 serving 복귀 절차에서 AOF를 생성해야 한다. 이 도구는 기존 serving 볼륨을 덮어쓰거나 AOF를 삭제하지 않는다.

## 실행 증거

2026-09-07 01:46 UTC GCP read-only 집계: Redis7.4.11, used_memory2,077,848B, RSS3,792,896B, limit128MiB, 최근fork3,274μs/COW1,245,184B/저장1초, AOF 정상/진행 중 저장0. MemAvailable2,409,308,160B/가용디스크5,781,733,376B. 기존 PG timer active, 최근01:31:06UTC 성공. 원문 데이터 조회·GCP mutation0. `/tmp/map-redis-backup-readonly-20260906.json`에 보존했다.

local fixture는 새 Redis 컨테이너만 사용해 string/hash/stream/TTL 및 기본16개 범위 밖의 DB19를 저장한다. 만료 이후 replica 집계와 일반 primary 집계 차이, 복사본 checksum·재복원, fixture 정리를 확인한다. transport는 local 복사로 대체하므로 GCS 검증으로 판정하지 않는다.

```sh
python3 -m unittest discover -s tests -p test_redis_backup.py -v
python3 scripts/verify-redis-backup-fixture.py
```

최종 local 실행은 [`redis-backup-fixture-20260906.json`](redis-backup-fixture-20260906.json)에 보존했다. Redis7.4.11/linux-arm64, databases32, DB0/2/4/6/19 총5키 snapshot과 만료후4키 primary 복원, 복사본 재복원 PASS, 3회 복원 합1.693초, fixture 제거 완료다. GCP 실제 linux-amd64 원격 저장/복원과 timer 활성화 증거는 아직 아니다. 전체 Infra162 tests와 신규12개 표적 회귀를 실행해 통과했다.
