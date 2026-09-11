# NCP 운영 exporter 계약

관리자와 중앙 Prometheus/Grafana는 기존 GCP에 유지한다. `docker-compose.prod-exporters.yml`은 NCP 운영의 PostgreSQL·Redis·호스트 계측만 정의한다. 기존 `docker-compose.target-exporters.yml`의 시험 설정은 변경하지 않는다.

운영 receiver가 만든 `map-prod-net`을 외부 네트워크로 사용하며 새 네트워크나 DB를 자체 생성하지 않는다. 세 이미지의 immutable registry digest를 `PROD_POSTGRES_EXPORTER_IMAGE`, `PROD_REDIS_EXPORTER_IMAGE`, `PROD_NODE_EXPORTER_IMAGE`에 넣고, 승인된 NCP artifact inventory의 같은 서비스와 일치시켜야 한다. Compose 보간은 digest 진위를 검증하지 않으므로 이 파일을 단독으로 `up`하는 것은 인수 경로가 아니다. 빈 호스트 User DB bootstrap 전용망에는 연결하지 않는다.

`PROD_POSTGRES_EXPORTER_DSN`은 `map_prod`의 별도 `pg_monitor` 로그인, Redis 입력은 별도 운영 metrics ACL이다. 운영 앱·bootstrap·migration·Admin 자격을 재사용하지 않는다. 시험용 `TARGET_*` 입력을 운영에 상속하지 않는다. 파일 권한은 root0600이며 실제 값과 Compose 전체 렌더링을 로그나 공개 artifact에 남기지 않는다.

각 exporter는 64MiB/0.25CPU, 읽기 전용 rootfs, `cap_drop=ALL`, `no-new-privileges`로 제한한다. node exporter만 호스트 PID와 `/host` 읽기 전용 mount를 가진다. 원시 포트 19187·19121·19100은 모두 `127.0.0.1`에 고정한다. 공인 포트 및 무인증 scrape를 추가하지 않는다.

원격 접속은 아직 활성화하지 않았다. GCP→NCP의 서버 신원 검증·암호화·접근 제한을 갖춘 통로를 설치한 뒤, 중앙 scrape에 `map_environment=prod`와 실제 호스트 식별자를 명시해야 한다. 기존 GCP test job 및 control DB는 유지한다. DB 브라우저의 최소 읽기 전용 SQL과 Redis 진단 ACL도 같은 환경 경계를 따라 별도로 인수한다. 통로 장애 때 test/control 데이터로 대체하지 않는다.

검증: `python3 -m unittest discover -s tests -p test_prod_exporters.py -v`는 실제 Compose CLI로 운영 project/network, loopback 포트, 권한·자원 제한과 누락 입력 거절을 확인한다. Docker daemon·이미지 pull·실제 운영 자격을 사용하지 않는다. 실제 이미지 보안, metrics ACL, 원격 TLS, GCP scrape, 연결 중단/복구 및 알림 수신은 이 검사의 PASS에 포함되지 않는다.
