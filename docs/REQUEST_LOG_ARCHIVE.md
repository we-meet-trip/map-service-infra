# 운영 요청 기록 보관 (log-6m/)

운영 컨테이너(proxy·user·hub·agent·yolo·edge)의 표준 출력·오류 로그를 json-file 순환으로 지워지기 전에 모아, 좌표를 가리고 암호화해 `gs://map-prod-archive/log-6m/requests/` 에 올린다. 보관소 수명 규칙이 생성 184일 뒤(그리고 Custom-Time 184일 뒤) 지운다. Cloud Logging 은 거치지 않는다.

| 조각 | 위치 |
|---|---|
| 수집기 | `scripts/request_log_archive.py` (표준 라이브러리, root) |
| 좌표 가림 | `scripts/coordinate_redaction.py` (옛 환경 보관 P5.2 와 같은 규칙) |
| 유닛·타이머 | `deploy/gcp/map-prod-reqlog-backup.service`·`.timer`(10분마다) |
| 설정 | `/etc/map-request-log/config.json` (root 0600, 한 번 쓰고 고치지 않음) |
| 상태 | `/var/lib/map-request-log/state.json` (커서·건수·객체 이름만, 로그 본문 없음) |

## 동작

- 서비스마다 `docker logs -t --since <커서+1ns> --until <지금-10초>` 로 창을 읽는다. 창은 겹치지도 비지도 않는다. 16 KiB 를 넘는 메시지는 Docker 가 조각마다 시각을 붙여 내보내므로 먼저 이어 붙인다.
- 줄마다 `coordinate_redaction.scrub` 으로 가린다(봉투 `v1.<iv>.<암호문>`, OSRM 경로 좌표, 좌표 이름의 값, 한국 범위 소수). 그래도 남으면 그 창은 올리지 않고 실패한다(커서는 그대로).
- 줄을 `{"ts","service","stream","line"}` NDJSON → gzip → `age -r <수신자>` 로 메모리에서만 바꾼다. 평문은 디스크에 닿지 않는다.
- 메타데이터 서버 토큰(VM SA)으로 JSON API `objects.insert` 를 부른다(`ifGenerationMatch=0`, Custom-Time = 올린 시각, md5 검증). VM SA 는 `log-6m/` 에 새 객체를 만드는 것만 되므로 `gcloud storage cp` 는 쓰지 않는다. 같은 창을 다시 올리면 412 이고 이미 올라간 것으로 본다.
- 출력은 서비스별 코드·건수·객체 이름과 `MAP_REQLOG_RESULT=OK|FAILED` 뿐이다. 유닛 이름이 Ops Agent 의 백업 유닛 패턴에 맞아 실패하면 백업 실패 경보가 난다. `MAP_BACKUP_RESULT` 는 쓰지 않는다(DB 백업 부재 경보를 가리지 않게).
- 실패 코드: `not_armed`, `wrong_host`, `container_not_single`, `log_config_changed`(json-file 10m×3 이 아님), `docker_logs_failed`, `residue_after_wide_mask`, `upload_<HTTP>`, 그리고 `gap`(앞 창의 마지막 줄이 순환으로 사라졌거나 컨테이너가 바뀜 — 그 사이 줄을 잃었을 수 있다).

## 설치 (별도 고정 체크아웃, P2.5 뒤·window.reserved 전에 한 번)

운영 계층(`/opt/map-ops-gcp`)의 고정 SHA 는 건드리지 않는다.

```sh
SHA=<세션 C 가 승인해 고정한 커밋>
sudo git clone --no-checkout https://github.com/we-meet-trip/map-service-infra.git /opt/map-reqlog
sudo git -C /opt/map-reqlog checkout --detach "$SHA"
sudo git -C /opt/map-reqlog rev-parse HEAD 'HEAD^{tree}'          # 고정값과 대조
sha256sum /opt/map-reqlog/scripts/coordinate_redaction.py          # 가림 규칙 해시를 결정 기록과 대조
sudo install -m 0644 /opt/map-reqlog/deploy/gcp/map-prod-reqlog-backup.service /opt/map-reqlog/deploy/gcp/map-prod-reqlog-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload                                       # 타이머는 켜지 않는다
sudo install -d -m 0700 /etc/map-request-log /var/lib/map-request-log
```

설정(`/etc/map-request-log/config.json`, root 0600):

```json
{"gcp_project": "mapcenter-b59ca", "instance": "map-prod", "bucket": "map-prod-archive",
 "prefix": "log-6m/requests/", "recipient": "<prod-keys-escrowed outputs.backup_age_recipient>",
 "compose_project": "map-prod", "services": ["proxy", "user", "hub", "agent", "yolo", "edge"],
 "settle_seconds": 10, "log_config": {"type": "json-file", "max-size": "10m", "max-file": "3"}}
```

## 켜기 (전환 공개 때)

리허설 기간에는 무장하지도 켜지도 않는다. 보관소의 `log-6m/` 은 VM 이 지울 수 없어, 시험 기간 줄이 한 번 올라가면 184일 남는다.

1. P3 ⑥ 직전(T-30m 에 앱을 멈춘 직후의 시각): `sudo python3 /opt/map-reqlog/scripts/request_log_archive.py arm --start <그 시각 UTC>`. 그 전 줄은 올라가지 않는다.
2. P3 ⑧5 공개 때: `sudo systemctl enable --now map-prod-reqlog-backup.timer`.
3. 확인: `sudo python3 /opt/map-reqlog/scripts/request_log_archive.py status`, `journalctl -u map-prod-reqlog-backup.service`.

## 컨테이너를 다시 만들기 전

compose up 으로 컨테이너가 다시 만들어지면 그 컨테이너의 json-file 도 함께 지워진다. 이미지·환경 변경, 전환 되돌림, VM 정지 전에는 먼저 한 번 모은다.

```sh
sudo systemctl start map-prod-reqlog-backup.service && journalctl -u map-prod-reqlog-backup.service -n 20 --no-pager
```

간격 10분은 컨테이너마다 10분에 10 MB 미만을 쓸 때 안전하다(순환 파일 3개 중 최근 2개 이상이 늘 남는다). P2.3 부하 때 컨테이너별 바이트/분을 재서 넘으면 타이머 간격을 줄인다.

## 수용 시험 (window.reserved 전, 세션 C)

운영 커서와 섞이지 않게 별도 설정·상태로 돌린다.

1. 시험 설정: 위 설정에서 `compose_project` 를 `map-reqlog-acceptance`, `services` 를 `["fixture"]`, `prefix` 를 `log-6m/acceptance/` 로 바꿔 `/root/reqlog-acceptance/config.json`(0600)에, 상태는 `/root/reqlog-acceptance/state`(0700).
2. 이미 받아 둔 운영 이미지 하나로 픽스처 컨테이너를 띄운다(`--pull=never --network none --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 --label com.docker.compose.project=map-reqlog-acceptance --label com.docker.compose.service=fixture`). 출력: `loc=` 봉투, `lat`/`lng`·`start_lat`, `x`/`y`, `SX`~`EY`, OSRM 경로 좌표, nginx `request: "GET …?lat=&lng="`, Caddy `"uri"`, JSON/dict 꼴, `%2C`·`%3B` 변형, 16 KiB 를 넘는 줄 1개, 대조군(IPv4·시각·지연).
3. `arm --start <픽스처 시작 전 시각>` → `run`(둘 다 `--config`·`--state-dir` 지정): 코드 `ok`, 줄 수 일치, 객체 이름이 `log-6m/acceptance/…` 인지 본다.
4. 소유자 계정으로 맥에서 암호문을 받아(`gcloud storage cat`) IAP SSH 표준 입력으로 VM 에 흘려 보내, VM 에서만 복호(`age --decrypt --identity <prod-backup-age-identity>`)·압축 해제한 뒤 `python3 -B /opt/map-reqlog/scripts/coordinate_redaction.py` 에 통과시켜 종료 코드 0(잔존 0)을 확인한다. 평문은 VM 의 파이프 밖에 두지 않는다.
5. 픽스처 컨테이너를 정확한 이름으로 지우고, 수용 객체는 기록한 뒤 소유자가 지운다.
6. GO#1 에서는 타이머 disabled, 상태 파일 없음, 설치 SHA·가림 규칙 해시만 다시 본다.

## 동의 철회와 만료 점검 (소유자 계정)

- 철회: 다음 수집 1회가 끝난 뒤, 해당 이용자의 줄이 든 객체를 내려받아 VM 에서만 복호하고 그 줄을 지운 뒤 다시 암호화해 새 이름으로 올린다(`ifGenerationMatch=0`, Custom-Time 은 원 객체의 Custom-Time). 그다음 원 객체를 정확한 이름으로 지운다. 이용자의 줄을 고르는 기준(계정→IP·시각 등)은 세션 C 가 정한다. 객체 이름에 식별자를 넣지 않는다.
- 만료: 수명 삭제는 비동기라 시한이 없다. 5일보다 짧은 간격으로 `log-6m/` 에서 생성(또는 Custom-Time) 184일이 지난 객체가 남았는지 나열하고, 있으면 정확한 이름으로 지운다.
