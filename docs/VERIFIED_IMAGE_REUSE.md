# 동일 소스의 검증 이미지 재사용

2026-09-07 R2 캐시 과정에서 containerd가 압축 blob과 압축 해제한 filesystem을 함께 보관함을 실측했다. uncompressed layer만 합산한 사전 예상은 3.14GB 잔여였지만 네 이미지 수신 뒤 실제 잔여는 2.066GB로 내려가 2GiB 하한에서 추가 수신을 중단했다. serving·DB·OSRM을 변경하지 않았다. 새 build runner가 같은 의존성을 매번 설치하면서 동일 source에도 새로운 layer digest를 만드는 점이 반복 배포의 디스크 점유를 키웠다.

수동 image-release의 선택 입력 `reuse_run_id`는 24시간 이내의 성공한 동일 저장소 release artifact를 검증해, 소스 SHA가 완전히 같은 서비스만 `FROM registry/image@sha256:...`로 재사용한다. 기본값은 비어 있어 기존 전체 빌드를 유지한다. 소스가 바뀐 서비스는 원래 Dockerfile로 빌드한다. 변경 없는 서비스에도 새 OCI version label과 새 release provenance를 붙여 새 digest를 생성한다.

검증은 생략하지 않는다.

- 실행 workflow가 기존 `deploy-gcp.py prepare`로 GitHub 성공 run/path/origin/artifact SHA256·bundle checksum·Compose 정합을 확인한다. 배포를 실행하지 않는다.
- helper는 최근 24시간, 동일 source SHA, clean tracked/untracked source를 확인한다. 서비스 repo와 image registry는 release schema의 고정 allowlist를 따른다.
- 재사용하는 실제 registry descriptor·linux/amd64 플랫폼·기존 revision/source/version label을 다시 검사한다. 생성 Dockerfile에는 검증된 digest의 FROM 한 줄만 들어간다.
- 새 여섯 이미지의 registry provenance 검증과 strict HIGH/CRITICAL scan 및 CRITICAL gate를 그대로 실행한다. 이전 검사 결과만 복사해서 통과시키지 않는다.
- 기존 runtime의 entrypoint/command/user/env/healthcheck/filesystem을 상속하므로 filesystem DiffIDs 보존과 OCI config 차이를 배포 전에 대조한다. 새 소스와 다른 이미지 또는 잘못된 플랫폼을 재사용할 수 없다.

이는 의존성을 새로 갱신하는 릴리스에 쓰는 기능이 아니다. base/dependency 재검증이 필요하면 입력을 비우고 빌드한다. 향후 용량 계산은 새 uncompressed layer뿐 아니라 압축된 OCI blob과 임시 쓰기 여유도 포함해야 한다. 오래된 이미지 정리는 현재·직전 복귀 이미지와 데이터 볼륨을 보호하고, 별도 checksum/복원 검증을 거친 후보에 한정한다.
