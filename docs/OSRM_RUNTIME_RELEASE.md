# 실제 OSRM 그래프 릴리스

한국 전체 foot/bicycle 그래프를 각각 빌드하고, GCP/NCP의 사용자 serving 호스트는
완성된 immutable artifact만 읽는다. build와 serving의 메모리 요구를 혼동하지 않는다.
`scripts/osrm-rebuild.sh`는 이제 안전한 빌더의 진입점이며 기존 볼륨 덮어쓰기를 거절한다.

## 원본과 엔진

- 엔진: Project-OSRM v26.5.0, linux/amd64,
  `ghcr.io/project-osrm/osrm-backend@sha256:aa6a1de3a71dafffd0ba39340542524f66e6841fc19bf7874a0e6a7967837f56`.
- 원본: [Geofabrik South Korea](https://download.geofabrik.de/asia/south-korea.html)의
  날짜가 고정된 PBF와 HTTPS로 확인한 provider MD5. 별도로 SHA256·원본 데이터 시각을
  기록한다. `latest` URL, 출처 미확인 기존 graph, MD5만 있는 runtime artifact는 거절한다.
- [foot profile](https://github.com/Project-OSRM/osrm-backend/blob/v26.5.0/profiles/foot.lua),
  [bicycle profile](https://github.com/Project-OSRM/osrm-backend/blob/v26.5.0/profiles/bicycle.lua)는
  별도 extract에서 사용하며 각각의 checksum과 lib/*.lua checksum을 보존한다.
- [StorageConfig v26.5.0](https://github.com/Project-OSRM/osrm-backend/blob/v26.5.0/include/storage/storage_config.hpp)의
  공통 16개 + MLD 4개 = profile별 20개, 총 40개 runtime 파일 전부를 보관한다.
  geometry/turn-guidance 기능을 끄거나 파일을 줄여 PASS시키지 않는다. PBF와
  extract/partition 중간 파일도 별도 build 볼륨에 보존한다.
- OSRM의 요청 URL profile 문자열은 그래프의 이동수단을 바꾸지 않는다.
  별도 엔진/프로파일 정체성과 응답 step mode를 함께 검사한다.
  [공식 API](https://project-osrm.org/docs/v26.4.0/api/)의 좌표는 longitude,latitude다.

## 별도 빌드 자리

예시의 source MD5는 실행 시점 공식 날짜 파일과 다시 대조한다. output과 volume은
매 시도 새 이름을 사용한다. 이미 있는 serving/build 볼륨을 초기화하지 않는다.

```bash
./scripts/osrm-rebuild.sh \
  --source https://download.geofabrik.de/asia/south-korea-260905.osm.pbf \
  --md5 b5b789b1e7a403fb6b6f54ebeba5aa99 \
  --volume map-osrm-build-20260906-release \
  --output /tmp/map-osrm-release-20260906-release
python3 scripts/osrm-squashfs.py /tmp/map-osrm-release-20260906-release
```

빌더는 available RAM 5GiB, free swap 6.25GiB, 디스크 10GiB 이상을 먼저 요구한다.
extract/partition/customize는 CPU1/RAM4.5GiB/RAM+swap10.5GiB 상한으로 순차 실행한다.
`build-stages.jsonl`에 단계별 exit/OOM/sample된 cgroup memory.peak를 기록하고,
실패 컨테이너·볼륨·로그도 보존한다. source만 재사용하려면 `--source-volume`을
사용하며 readonly mount 후 MD5/SHA256을 다시 확인한다.

2026-09-06 로컬 시험에서는 RAM4.5GiB/swap0.5GiB 제한으로 extract가 exit137/OOM이었다.
추가로 RAM4.5GiB/swap2GiB 제한도 extract exit137/OOM이었다.
이후 threads1·RAM4.5GiB/swap6GiB로 변경하여 두 profile의 6단계가 모두 exit0/OOMfalse로 완료됐다.
extract의 sampled swap peak는 foot 3,318,738,944B, bicycle 3,213,524,992B였다.
빌드 뒤 신규 swap 2개를 안전 여유 확인 후 swapoff하고 신규 scratch 파일만 제거했다.
따라서 작은 GCP serving 호스트에서 전처리를 수행하지 않는다. 임시 swap은 운영
서버에 자동 생성하는 기능이 아니다. 로컬 Docker Desktop에서만 기술 검토 뒤 신규
전용 볼륨에 2GiB + 4GiB 파일을 만들고 제한 SYS_ADMIN으로 활성화했다. 기존 볼륨은 불변이다.
버퍼 쓰기는 작은 helper memory cap을 초과할 수 있으므로 직접 I/O로 예약했다.
빌드 종료 뒤 swap 사용량과 available RAM을 검사하고, 사용량을 안전하게 RAM으로
옮길 수 있을 때만 해당 파일을 swapoff한다. 여유가 없으면 swapoff를 강행하지 않는다.
파일/볼륨 제거는 별도 보존 범위 확인 후 수행하며 기존 swap은 변경하지 않는다.

## 압축 runtime과 호스트 설치 계약

manifest의 `archive.filename`(`osrm-runtime.tar.gz` 또는 권한 수정본 `osrm-runtime-nonroot.tar.gz`)은
전체 40개 파일을 검증한 전달/복구본이다. extract의 `fileIndex`는 root-only 모드로
생성될 수 있으므로 새 build volume의 runtime 40개 파일만 0644로 정규화한다.
archive와 mount 검증 모두 0644를 강제하여 nonroot 엔진의 초기 기동 실패를 예방한다.
`osrm-runtime.squashfs`는 같은 40개 파일을 zstd 압축한 read-only filesystem이며,
serving에서는 이를 loop mount하고 OSRM mmap으로 읽는다. 압축본과 풀린 복사본을
동시에 GCP 디스크에 저장할 필요가 없다. 설치 전 kernel squashfs/loop 지원과 실제
디스크·메모리·swap 여유를 확인해야 한다. mount만으로 경로 PASS를 선언하지 않는다.

호스트별 독립 설치/수령자는 다음 순서를 지킨다.
`scripts/osrm-systemd.py`로 먼저 mount/service/runtime.env를 지정한 output에 렌더링한다.
`--infra-dir`에는 버전별 독립 tooling 디렉터리를 사용할 수 있어 앱 checkout 변경과
분리한다. 이 도구는 호스트를 변경하지 않으며 unit의 graph 전체 checksum 선행 검사와 mount
의존성, routing project만 stop하는 복귀 명령을 함께 만든다.

1. 앱 배포와 조율한 lock 아래 기존 16개 컨테이너 identity·사용자 지문·백업·자원
   기준선을 저장한다. 공개 graph artifact 외 사용자 원문을 출력하지 않는다.
2. 새 release 디렉터리에 `.partial`로 전송하고 SHA256을 manifest와 대조한 뒤 rename한다.
   기존 release·Docker 볼륨·PG/Redis·edge 이미지는 변경하지 않는다.
3. 새 전용 mountpoint에 `loop,ro,nodev,nosuid,noexec` SquashFS mount를 적용한다.
   `python3 scripts/osrm-release.py verify-tree manifest.json <mountpoint>`로 전체 40개 파일의
   이름·일반파일 여부·크기·SHA256을 확인한다. engine digest도 동일해야 한다.
4. `docker-compose.osrm-runtime.yml`을 독립 project `map-routing-test` 또는
   `map-routing-prod`로 기동한다. `MAP_ENVIRONMENT`, `OSRM_NETWORK`, `OSRM_MOUNT_ROOT`,
   `OSRM_FOOT_PORT`, `OSRM_BICYCLE_PORT`는 해당 환경 값으로 명시한다. serving network에
   `osrm-foot`/`osrm-bicycle` alias를 제공한다. port는 loopback만 바인딩한다.
5. mount unit은 boot 때 Docker 라우팅 service보다 먼저 시작하고 routing service는
   mount에 Requires/After/BindsTo를 둔다. 네트워크는 해당 serving host의 Docker network다.
   앱 receiver는 이 graph mount/독립 라우팅 project를 재생성하거나 제거하지 않는다.
6. 각 엔진은 nonroot/readonly/cap-drop-all/CPU0.5/RAM512MiB/swap0으로 기동한다.
   실제 peak/RSS/page-cache·지연·OOM을 측정하고 한도 적합성을 판정한다.
   v26.5.0의 request_handler는 access log와 WARNING 예외에 원문 좌표 URI를
   기록하므로 `DISABLE_ACCESS_LOGGING=1`, `--verbosity ERROR`를 함께 고정한다.
   원문을 기록하는 WARNING 대신 Hub의 안전한 오류 코드·health/latency로 관측한다.
7. 실제 engine 검증 후 해당 환경 Hub의 `OSRM_FOOT_BASE_URL=http://osrm-foot:5000`,
   `OSRM_BICYCLE_BASE_URL=http://osrm-bicycle:5000`을 적용한다. User/Agent/Client의
   측정/추정 구분, 좌표 변환, 수동 순서·optimize·저장/재조회/편집 시간표를 별도 검증한다.

복귀는 해당 환경 Hub의 이전 설정을 복원하고 **라우팅 project만 stop**한 뒤 해당
mount를 해제한다. 신규 artifact와 이전 graph release를 보존한다. serving 전체
`compose down`, volume 삭제, 기존 DB 재생성, GCP 시험 DB로 운영 주소 전환은 복귀가 아니다.
자동 복구 service/mount unit을 비활성화해야 정지한 그래프가 boot 때 임의로 재기동하지 않는다.

## 실제 경로 검사

```bash
python3 scripts/osrm-acceptance.py \
  --foot http://127.0.0.1:5200 --bicycle http://127.0.0.1:5201 \
  --data-version 2026-09-05T20:22:06Z --requests 24 \
  --output osrm-acceptance.json
```

서울의 3점 수동 순서, 부산·제주를 양 profile로 검사한다. 실제 geometry≥10점,
OSM node annotation, 요청 시종점 snapping, leg별 순서, lat/lng 규약, 양의 유한
거리·시간, geometry 길이와 거리 일치, 직선보다 유의미한 곡률, step mode, source
timestamp를 검사한다. 반복 요청은 concurrency2/최대100으로 제한한다. 이 검사는
사용자 계정 생성이나 외부 유료 API를 호출하지 않는다.

OSRM 시간은 도로 그래프와 profile 속도/페널티로 계산한 **모델 기반 경로 시간**이며
실시간 교통 측정값이 아니다. 장소 실존, 실제 통행 허가/공사/영업시간, 킥보드의
합법적 통행까지 보장하지 않는다. foot/bicycle source를 정확히 표시하고 실패를
추정 경로의 정상 실측 성공으로 바꾸지 않는다.

코드 단위 검사는 `python3 -m unittest discover -s tests -p 'test_osrm*.py' -v`로 실행한다.
최신 실제 stage/image/artifact/GCP 증거는 루트 `evidence/runtime/osrm-*20260906*`에 기록한다.
