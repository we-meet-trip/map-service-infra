# 운영 요청 기록 보관 (log-6m/)

설정한 운영 컨테이너(권장 proxy·user·hub·agent·yolo·edge)가 표준 출력·오류에 쓰는 줄 전부를 json-file 순환으로 지워지기 전에 모은다. 줄마다 좌표를 가리고 암호화해 `gs://map-prod-archive/log-6m/requests/` 에 올린다. 접근 줄(IP·시각·경로·검색어·주소 문자열)과 오류·추적 줄이 모두 들어가며, 좌표 말고는 그대로 남는다. 보관소 수명 규칙이 생성 184일 뒤, 또는 Custom-Time 184일 뒤 가운데 이른 때 지운다. 줄은 Cloud Logging 을 거치지 않는다. journald 를 거쳐 백업 경보 지표로 가는 것은 수집기 자신의 결과 줄(코드·건수·객체 이름)뿐이다.

| 조각 | 위치 |
|---|---|
| 수집기 | `scripts/request_log_archive.py` (표준 라이브러리, root) |
| 좌표 가림 | `scripts/coordinate_redaction.py` (옛 환경 보관 P5.2 와 같은 규칙) |
| 유닛·타이머 | `deploy/gcp/map-prod-reqlog-backup.service`·`.timer`(10분마다) |
| 설정 | `/etc/map-request-log/config.json` (root 0600, 한 번 쓰고 고치지 않음) |
| 상태 | `/var/lib/map-request-log/state.json` (서비스별 커서·컨테이너 ID·진행 중인 창·마지막 줄 시각·끝까지 읽음 표시, 마지막 실행 코드. 로그 본문 없음) |
| 선행 | 보관소 접두어 수명 규칙과 VM SA 의 `log-6m/` 생성 전용 권한(`feature-prod-audit-archive-retention`, 운영에 적용됨) |

## 동작

- 서비스마다 창 (커서, 지금 − 10초] 을 `docker logs -t --since <ns> --until <ns>` 로 읽는다(양 끝 포함). 창은 겹치지도 비지도 않는다. 멈춘 컨테이너는 지금까지 읽고 끝까지 읽은 것으로 표시한다. 일회성 `compose run` 컨테이너는 대상이 아니다.
- settle(10초)보다 늦게 json-file 에 닿은 줄과 Docker 가 해독하지 못한 레코드는 조용히 빠진다.
- 창(시작·끝)은 읽기 전에 상태 파일에 먼저 적는다. 업로드 중 끊기거나 유닛이 죽어도 다음 실행이 같은 창을 같은 객체 이름으로 다시 한다. 이미 올라갔으면 412 를 받고 올라간 것으로 본다(운영 VM 에서 이 권한으로 같은 이름을 다시 만들면 412 임을 실측했다).
- 16 KiB 를 넘는 메시지는 Docker 가 16 KiB 조각마다 같은 시각을 붙여 줄바꿈 없이 내보낸다. 조각 경계에서 잘린 한글 같은 여러 바이트 문자는 U+FFFD 로 저장되고 뒤 경계를 밀어낸다. 그래서 첫 조각 뒤의 반복 시각을 위치가 아니라 값으로 지워 잇는다(잘린 문자 하나는 되살릴 수 없다).
- 줄마다 `coordinate_redaction.scrub` 을 적용한다.
  1. 1차 규칙으로 봉투, 64자리 해시, OSRM 경로, 좌표 이름의 값, 한국 범위의 소수 3자리 이상 수를 가린다.
  2. 이스케이프를 한 번 푼 사본에 잔존 검사가 걸리면 같은 느슨한 규칙으로 가린다.
  3. 그래도 걸리면 모든 이스케이프, IPv4[:포트] 를 뺀 소수점 있는 수, 64자 이상 16진수, 봉투, 좌표 이름의 값을 가린다.

  3 의 각 단계는 글자를 `*` 로만 바꿔 앞 단계가 지운 것을 되살리지 못하므로 3 뒤에는 잔존이 남을 수 없다. 근거는 `_strip` 주석에 있고, 무작위 입력(저장소 시험 6만 줄, 문법 기반 검사 282만 줄)으로도 확인했다. 줄의 접속 IP 는 남는다. 줄 전체를 바꾸는 WITHHELD 는 닿지 않는 안전장치다. 닿으면 코드 `withheld` 로 보고하지만 창은 올리고 커서는 나아간다. 어떤 줄도 수집을 멈추지 못한다.
- `{"ts","service","stream","line"}` NDJSON → gzip → `age -r <수신자>` 를 메모리에서만 한다. 평문은 디스크에 닿지 않는다.
- 메타데이터 서버 토큰(VM SA)으로 JSON API `objects.insert` 를 부른다(`ifGenerationMatch=0`, Custom-Time = 올린 시각, md5 검증). VM SA 는 `log-6m/` 에 새 객체를 만드는 것만 되므로 목적지를 먼저 읽는 `gcloud storage cp` 는 쓰지 않는다.
- 서비스 하나가 어떤 이유로 실패해도 그 서비스의 코드가 될 뿐이고 다른 서비스는 계속 돈다. 실행이 겹치면 뒤 실행이 앞 실행을 기다린다.
- 수동 수집은 유닛으로만 한다(샌드박스·저널·지표가 같게). 진행 중인 타이머 실행이 있으면 `systemctl start` 가 그 실행에 합쳐지고 새 실행을 하지 않는다. 그래서 '멈춘 뒤 모으기'는 `sudo systemctl start map-prod-reqlog-backup.service` 를 두 번 잇달아 한다. 두 번째가 멈춘 뒤에 시작한 실행을 보장한다. 그다음 `status` 에서 그 서비스가 `drained=True` 인지 본다. 앞 시도가 남긴 창(`pending`)을 마저 올린 실행은 끝까지 읽음으로 치지 않으므로, `False` 면 한 번 더 한다.
- 출력은 서비스별 `MAP_REQLOG service=… code=… lines=… masked=… withheld=… object=… status=…` 와 `MAP_REQLOG_RESULT=OK|FAILED` 다. 오류는 코드나 예외 형식 이름만 찍고 메시지는 찍지 않는다. 모든 서비스가 ok 일 때만 `MAP_BACKUP_RESULT=COMPLETE kind=reqlog` 를 찍는다. 백업 부재 경보가 종류(kind)별로 75분을 보므로 수집기가 멈추면 이 경보가 난다. 실패한 실행은 유닛 실패로 끝나고, 유닛 이름이 Ops Agent 백업 패턴에 맞아 백업 실패 경보가 난다.
- 서비스 코드:
  - `ok`
  - `withheld`
  - `gap`: 앞 창의 마지막 줄이 순환으로 사라졌거나(`rotation_gap`), 끝까지 읽지 않은 컨테이너가 바뀌었거나(`lost_on_replace`), 시계가 커서보다 뒤로 갔다(`clock_behind`). 그 사이 줄을 잃었을 수 있다.
  - `container_not_single`
  - `log_config_changed`: json-file 의 max-size·max-file 말고 다른 옵션이 있거나 값이 다르다.
  - `inspect_failed`, `docker_logs_failed`, `unparsed_log_record`, `age_failed`, `upload_<HTTP>`, `upload_mismatch`
  - 예상 밖 예외의 형식 이름(`URLError` 등)

  실행 전체 코드: `not_armed`, `wrong_host`, `config_*`, `state_dir_not_private`, 예상 밖 예외의 형식 이름(`FileNotFoundError` 등)
- 알려진 한계(보고되지 않는 유실):
  - 끝까지 읽은 컨테이너를 `docker start` 로 다시 켰다가 다음 수집 전에 다시 만들면, 다시 켠 동안의 줄은 잃고 `gap` 도 나지 않는다.
  - 무장 직후나 교체 직후의 첫 창은 비교할 앞 줄이 없다. 그 창이 올라가기 전에 순환이 지나가면(긴 장애 — 그동안 매 실행이 실패 경보를 낸다) 잃은 줄은 보고되지 않는다.
  - 시계가 뒤로 간 것은 그 동안 수집이 돌 때만 `clock_behind` 로 잡힌다. 유닛은 `After=time-sync.target` 으로 시간 동기화 뒤에 서도록 순서를 둔다(동기화 대기 서비스가 켜져 있어야 실제로 기다린다).

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

`arm` 이 유닛보다 먼저 상태 디렉터리를 쓰므로 설치 때 만든다. 유닛의 `StateDirectory=` 도 같은 경로·권한(root 0700)이다.

## 켜기와 끄기 (전환 창)

리허설 기간에는 무장하지도 켜지도 않는다. 보관소의 `log-6m/` 은 VM 이 지울 수 없어, 시험 기간 줄이 한 번 올라가면 184일 남는다.

1. P3 ⑥ 직전 VM 에서 다음을 실행한다. 시작 시각은 지금이고, 6시간보다 오래된 시작은 거부된다.

   ```sh
   sudo python3 -B /opt/map-reqlog/scripts/request_log_archive.py arm --start "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   sudo systemctl start map-prod-reqlog-backup.service
   ```

   두 번째 줄은 T-30m 에 멈춘 컨테이너를 끝까지 읽은 것으로 기록한다(`status` 의 `drained=True`). 창이 비어 객체는 생기지 않고, ⑥ 이 그 컨테이너를 다시 만들어도 `gap` 이 아니다. 실행 중이던 컨테이너(edge 등)를 ⑥ 이 다시 만들면 첫 실행이 그 서비스에 `gap` 을 한 번 낸다.
2. ⑧5 에서 `sudo systemctl enable --now map-prod-reqlog-backup.timer`. ⑥·⑦ 사설 검사의 줄은 첫 실행이 모은다.
3. GO#3 에서 확인한다.
   - `sudo python3 -B /opt/map-reqlog/scripts/request_log_archive.py status` 의 마지막 코드가 모두 ok 다.
   - `journalctl -u map-prod-reqlog-backup.service` 에 `MAP_REQLOG_RESULT=OK` 가 있다.
   - 지표 `map_backup_complete{kind=reqlog}` 에 첫 점이 들어왔다.
4. GCE 정지(RB-1·RB-2): 앱 컨테이너를 모두 멈춘 뒤 `sudo systemctl disable --now map-prod-reqlog-backup.timer` → `sudo systemctl start map-prod-reqlog-backup.service` 두 번(끝까지 읽기) → `status` 기록.

되돌린 뒤 다시 전환하거나 상태 파일을 새로 만들어야 하면, 타이머를 끈 상태에서 `state.json` 을 정확한 이름으로 옆에 옮겨 두고(지우지 않는다) 1번부터 다시 한다. 빈틈 없이 이어 모으려면 `--start` 를 옮긴 파일의 가장 이른 커서로 한다(6시간 안일 때만 된다). 그 경우 다른 서비스의 일부 줄은 두 객체에 겹쳐 들어간다.

## 컨테이너를 다시 만들 때

compose 가 컨테이너를 다시 만들면 그 컨테이너의 json-file 도 함께 지워진다. 마지막 수집 뒤의 줄을 남기려면 멈춘 다음 모으고 다시 만든다. compose 호출은 서빙 계층의 것을 그대로 쓴다.

```sh
<compose> stop <서비스>
sudo systemctl start map-prod-reqlog-backup.service
sudo systemctl start map-prod-reqlog-backup.service
sudo python3 -B /opt/map-reqlog/scripts/request_log_archive.py status   # 그 서비스 drained=True
<compose> up -d <서비스>
```

- 무중단 교체(`scripts/service-rollover.py`)의 임시 컨테이너는 `--log-driver=none` 이다. 그래서 교체하는 동안 그 서비스(hub·agent·yolo·user)의 줄은 어디에도 남지 않는다. 같은 요청의 proxy 줄은 남는다.
- 원래 컨테이너는 실행 중에 다시 만들어진다. 마지막 수집 뒤의 줄을 잃을 수 있고, 다음 실행이 `gap` 을 보고한다(백업 실패 경보). 이를 없애려면 교체 도구에 단계를 넣어야 한다. 트래픽이 임시 컨테이너로 옮겨진 뒤 원래 컨테이너를 멈추고, 수집을 한 번 돌린 다음, 다시 만드는 단계다(세션 C 결정 사항).
- 간격 10분은 컨테이너마다 10분에 10 MB 미만을 쓸 때 안전하다. json-file 은 10 MB×3 이고 순환은 가장 오래된 파일만 지운다. P2.3 부하 때 컨테이너별 바이트/분을 재서 넘으면 타이머 간격을 줄인다.

## 수용 시험 (window.reserved 전, 세션 C)

운영 커서와 섞이지 않게 별도 설정·상태로 돌리되, 실제 유닛의 샌드박스를 통과하는지 같이 본다.

1. 시험 설정을 둔다. 위 설정에서 `compose_project` 를 `map-reqlog-acceptance`, `services` 를 `["fixture"]`, `prefix` 를 `log-6m/acceptance/` 로 바꿔 `/etc/map-request-log-acceptance/config.json`(디렉터리 0700, 파일 0600)에 둔다. `/root` 는 유닛의 `ProtectHome=yes` 로 보이지 않는다.
2. 이미 받아 둔 운영 이미지 하나로 픽스처 컨테이너를 띄운다.

   ```sh
   --pull=never --network none --log-driver json-file --log-opt max-size=10m --log-opt max-file=3
   --label com.docker.compose.project=map-reqlog-acceptance --label com.docker.compose.service=fixture
   --label com.docker.compose.oneoff=False
   ```

   출력할 줄:
   - `loc=` 봉투, `lat`/`lng`·`start_lat`, `x`/`y`, `SX`~`EY`
   - OSRM 경로 좌표, nginx `request: "GET …?lat=&lng="`, Caddy `"uri"`, JSON·dict 꼴
   - `%2C`·`%3B`·`%2B`·`%3D` 변형과 nginx `\x22` 꼴, 64자리 해시
   - 32 KiB 를 넘는 줄 1개. 첫 경계에 한글, 둘째 경계에 좌표를 둔다. 운영 엔진(29.8.2)에서 조각 형식을 확인하는 줄이다.
   - 대조군: IPv4·시각·지연·버전
3. 픽스처가 끝난 뒤 settle(10초)보다 오래 기다린다. 그다음 실제 유닛을 런타임 drop-in 으로 시험 설정에 돌린다.

   ```sh
   sudo python3 -B /opt/map-reqlog/scripts/request_log_archive.py --config /etc/map-request-log-acceptance/config.json --state-dir /var/lib/map-request-log-acceptance arm --start <픽스처 시작 전 시각>
   sudo systemctl edit --runtime map-prod-reqlog-backup.service
   #   [Service]
   #   StateDirectory=map-request-log-acceptance
   #   ExecStart=
   #   ExecStart=/usr/bin/python3 -B -X pycache_prefix=/run/map-prod-reqlog-backup /opt/map-reqlog/scripts/request_log_archive.py --config /etc/map-request-log-acceptance/config.json --state-dir /var/lib/map-request-log-acceptance run
   sudo systemctl start map-prod-reqlog-backup.service
   journalctl -u map-prod-reqlog-backup.service -n 20 --no-pager
   sudo systemctl revert map-prod-reqlog-backup.service
   ```

   `arm` 전에 `sudo install -d -m 0700 /var/lib/map-request-log-acceptance` 를 한다. 기대값은 다음과 같다.
   - 코드 `ok`, 줄 수 = 픽스처 줄 수
   - 객체 이름이 `log-6m/acceptance/…`
   - `MAP_REQLOG_RESULT=OK`
4. 소유자 계정으로 맥에서 암호문을 받는다(`gcloud storage cat`). IAP SSH 표준 입력으로 VM 에 흘려 보내 VM 에서만 처리한다.

   ```sh
   age --decrypt --identity <LUKS 위 0600 사본> | gunzip | python3 -B /opt/map-reqlog/scripts/coordinate_redaction.py --check
   ```

   `MAP_REDACTION_CHECK lines=<줄 수> residue=0`, 종료 코드 0 을 확인한다. 평문은 VM 의 파이프 밖에 두지 않고, identity 사본은 `shred -u` 한다.
5. 정리한다.
   - 픽스처 컨테이너를 정확한 이름으로 지운다.
   - `/etc/map-request-log-acceptance`·`/var/lib/map-request-log-acceptance` 를 지운다.
   - 수용 객체는 기록한 뒤 소유자가 정확한 이름으로 지운다.

   운영 경보는 GO#3 에 켠다. 그때는 수집기가 이미 10분마다 `kind=reqlog` 를 찍고 있으므로, 시험 실행이 남긴 점이 부재 경보를 만들지 않는다.
6. GO#1 에서는 수용 기록을 참조하고 다음만 읽어서 다시 본다.
   - 타이머 disabled·inactive, `/var/lib/map-request-log/state.json` 없음
   - 설치 SHA·트리, 가림 규칙 해시, config sha256
   - config 의 수신자 = prod-keys-escrowed 의 `backup_age_recipient`
   - `gcloud storage buckets describe gs://map-prod-archive --format='json(lifecycle_config)'` 의 `log-6m/` 규칙

## 일일 점검과 키 교체

- 매일 `status` 를 본다. 마지막 실행이 20분 안이고, `pending=False`, 코드가 모두 ok 여야 한다. `gap` 이 있으면 원인(교체·순환)을 기록한다.
- 첫날 실제 객체 하나를 수용 시험 4번 방식으로 복호 검사한다.
- 컨테이너별 json-file 증가량이 10분에 10 MB 에 가까워지면 간격을 줄인다. 타이머 drop-in(`OnCalendar=`)으로 바꾸고 drop-in 해시를 기록한다.
- Docker 엔진 판이 바뀌면 16 KiB 조각 형식과 `--since/--until` 경계를 수용 시험 2번의 긴 줄로 다시 확인한다.
- age 키를 교체하면 타이머를 끄고 `systemctl start` 두 번으로 모은 뒤 config 의 `recipient` 만 새 값으로 바꾸고(root 0600, sha256 기록) 다시 켠다. 상태 파일은 그대로 둔다.

## 동의 철회와 만료 점검 (소유자 계정)

- 철회는 다음 수집 1회가 끝난 뒤 한다.
  1. 해당 이용자의 줄이 든 객체를 고른다. 고르는 기준(계정 → IP·시각 등)은 세션 C 가 정한다. 객체 이름에는 식별자를 넣지 않는다.
  2. VM 에서만 복호하고 그 줄을 지운 뒤 같은 수신자로 다시 암호화한다. identity 는 LUKS 위 0600 사본을 쓰고 끝나면 `shred -u` 한다. 암호문만 맥으로 돌아온다.
  3. 맥에서 새 이름으로 올린다. Custom-Time 을 원 객체 값으로 둬서 만료가 그대로이게 한다.

     ```sh
     gcloud storage cp - gs://map-prod-archive/<새 이름> --if-generation-match=0 --custom-time=<원 객체 Custom-Time>
     ```
  4. 원 객체를 정확한 이름으로 지운다.
- 만료: 수명 삭제는 비동기라 시한이 없다. 이틀 이하 간격(매일 권장)으로 `log-6m/` 를 나열한다. Custom-Time 과 생성 시각 가운데 이른 쪽에서 184일이 지난 객체가 남았으면 정확한 이름으로 지운다. 공지의 파기 기한과 맞추는 것은 세션 C 가 정한다.
- 옛 환경 P5.2 는 json-file 원본을 읽는다. `"log"` 값이 `\n` 으로 끝나지 않는 레코드는 다음 레코드와 이어 붙인 뒤 필터에 넣는다. 잇지 않으면 경계의 좌표가 쪼개져 가려지지 않는다.
