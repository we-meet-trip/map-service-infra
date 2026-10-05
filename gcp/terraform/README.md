# GCP 기반 (OpenTofu)

| 환경 | 디렉터리 | 프로젝트 | 리전 | 상태 버킷 / 설정 버킷 |
|---|---|---|---|---|
| 운영 | `envs/prod` | mapcenter-b59ca | asia-northeast3 | map-prod-tfstate / map-prod-tf-config |
| 시험 | `envs/test` | mapservice-test | us-central1 | map-test-tfstate / map-test-tf-config |

`modules/host` 는 두 환경 공통(API·VPC·IAP SSH·고정 IP(Standard)·전용 SA·VM·이메일 채널·업타임 체크와 경보·대시보드), `modules/bucket` 은 버킷 하나와 그 권한 표 전체(`google_storage_bucket_iam_policy`)다.

## 판
- OpenTofu 1.12.6(brew `opentofu`), `required_version >= 1.12.0`.
- hashicorp/google·google-beta 8.5.0(`~> 8.5`). google-beta 는 소비자 쿼터 재정의에만 쓴다.
- `.terraform.lock.hcl` 은 OpenTofu 레지스트리 주소로 기록돼 Terraform 과 함께 쓸 수 없다. 갱신: `tofu providers lock -platform=darwin_arm64 -platform=linux_amd64`.

## 부트스트랩 (Terraform 밖, 환경마다 한 번)
두 버킷 모두 균일 접근·공개 차단·버전 켬. 상태 버킷은 소유자(apply 주체)만, 설정 버킷은 소유자와 팀 그룹(읽기)만 접근한다. 권한 표 전체를 덮어써 projectEditor·projectViewer 편의 바인딩을 없앤다. 소유자에게 `roles/storage.admin`(objectAdmin 포함)을 주는 이유: 기본 역할 owner 는 버킷 조회·정책 권한이 본래 없어 편의 바인딩이 사라지면 버킷을 다시 관리할 수 없다.

```sh
# 운영. 시험은 P=mapservice-test L=us-central1 E=test (시험 프로젝트 생성·결제 연결은 선행)
P=mapcenter-b59ca L=asia-northeast3 E=prod
OWNER=user:OWNER_EMAIL           # 실제 주소는 셸에서만 넣는다
T=$(mktemp -d /tmp/map-tf.XXXXXX)  # iCloud 밖
gcloud services enable cloudresourcemanager.googleapis.com serviceusage.googleapis.com --project=$P
for B in map-$E-tfstate map-$E-tf-config; do
  gcloud storage buckets create gs://$B --project=$P --location=$L --uniform-bucket-level-access --public-access-prevention
  gcloud storage buckets update gs://$B --versioning
done
printf '{"bindings":[{"role":"roles/storage.admin","members":["%s"]}]}\n' "$OWNER" > $T/iam.json
# 결과 정책에는 소유자 주소가 들어 있어 화면에 내지 않는다.
gcloud storage buckets set-iam-policy gs://map-$E-tfstate $T/iam.json > /dev/null
gcloud storage buckets set-iam-policy gs://map-$E-tf-config $T/iam.json > /dev/null
# 팀 그룹이 생기면 설정 버킷에만 읽기를 더한다(그때 주석을 풀고 TEAM_GROUP 을 그룹 주소로 바꾼다).
# gcloud storage buckets add-iam-policy-binding gs://map-$E-tf-config --member=group:TEAM_GROUP --role=roles/storage.objectViewer > /dev/null
# 비공개 변수: $E.tfvars.example 을 $T 에서 채워 올린다(저장소에 두지 않는다).
gcloud storage cp $T/$E.tfvars gs://map-$E-tf-config/$E.tfvars
rm -rf "$T"
```

## 계획과 적용
변수 파일·계획·로그·`.terraform` 은 모두 iCloud 밖 0700 임시 폴더(`mktemp -d`)에 둔다. ADC 는 소유자 계정(`gcloud auth application-default login`).
사람이 읽는 tofu 출력에는 비공개 값이 그대로 나온다(sensitive 변수도 계산된 ID·버킷 정책 JSON 에는 드러난다). 그래서 init·plan·apply 출력은 `$T` 의 로그로만 보내고, 화면·대화 기록·evidence 에는 `plan_check.py` 의 동작·주소 요약과 종료 코드만 옮긴다. 로그는 소유자가 직접 연다.

```sh
cd gcp/terraform/envs/prod                     # 시험은 envs/test, 이하 prod -> test
T=$(mktemp -d /tmp/map-tf.XXXXXX); export TF_DATA_DIR=$T/data
gcloud storage cp gs://map-prod-tf-config/prod.tfvars $T/prod.tfvars
tofu init -backend-config=bucket=map-prod-tfstate -backend-config=prefix=terraform/prod > $T/init.log 2>&1
tofu plan -var-file=$T/prod.tfvars -out=$T/prod.tfplan > $T/plan.log 2>&1
tofu show -json $T/prod.tfplan > $T/prod.tfplan.json
python3 -B ../../plan_check.py $T/prod.tfplan.json   # 0 이 아니면 적용하지 않는다
# 이 요약(동작·주소만, 값 없음)을 승인 요청에 붙이고 승인받은 뒤
tofu apply $T/prod.tfplan > $T/apply.log 2>&1
echo "apply exit $?"
rm -rf "$T"                                    # 실패했으면 지우기 전에 소유자가 $T/*.log 를 직접 본다
```

`plan_check.py` 는 디스크·VM·고정 IP·버킷·로그 버킷·WIF 풀/공급자의 삭제·교체, 이미 있는 `*-web-public` 의 꺼짐·삭제, 이미 켜진 경보 정책의 꺼짐·삭제·교체를 막는다(종료 코드 1). 처음 만드는 자원이 꺼진 상태(disabled=true·enabled=false)인 것은 정상이다. `--frozen` 은 아래 적용 금지 구간용이다.

운영 VM 은 `allow_stopping_for_update = false` 라 정지가 필요한 변경(machine_type·서비스 계정·Shielded VM 설정 등)이면 apply 가 실패한다. 다시 시작하면 데이터 디스크를 LUKS 로 직접 열어야 하므로(저장소 `docs/NCP_PRODUCTION_SERVING.md` 의 'Stop, reboot and backup acceptance') 이런 변경은 계획된 중단 창에서 따로 한다.

오프라인 검사(자격 증명 없음): 각 env 에서 `export TF_DATA_DIR=$(mktemp -d)/data; tofu init -backend=false && tofu validate && tofu test`, `tofu fmt -check -recursive gcp/terraform`, `python3 -B -m unittest gcp/terraform/test_plan_check.py`(저장소 최상위).

## 운영 적용 금지 구간
- `evidence/coordination/locks/window.reserved` 가 있으면 env-prod 의 apply 를 하지 않는다. 그 사이 운영 변경은 gcloud 로만 한다.
- cutover-done 관문이 있는데 `prod_public_web`·`prod_vm_alerts` 의 기본값을 true 로 바꾸는 변경이 아직 반영되지 않았으면 apply 하지 않는다. 기본값(false) 그대로 apply 하면 web-public 이 다시 꺼져 운영 전체가 멈추고 VM·백업 경보도 꺼진다.
- 부득이하면 `-var prod_public_web=true -var prod_vm_alerts=true` 를 더해 plan 하고 `python3 -B ../../plan_check.py --frozen $T/prod.tfplan.json` 으로 검사한다. `--frozen` 은 이미 있는 web-public 의 disabled 나 경보 정책의 enabled 가 어느 방향으로든 바뀌거나 그 자원이 지워지는 줄이 하나라도 있으면 1 을 낸다. 1 이면 중단한다(전환 창에서 gcloud 로 켜거나 끈 상태를 apply 가 되돌리지 않게 한다).

## 계산·예외·메모
- 업타임 월 실행 수(730시간, 체커 5 = USA 3 + ASIA_PACIFIC 1 + EUROPE 1): 운영 60초 2개 = 2 × 730 × 60 × 5 = 438,000, 300초 4개 = 4 × 730 × 12 × 5 = 175,200, 합계 613,200 ≤ 900,000(무료 한도는 프로젝트마다 월 100만). 시험은 300초 1개 = 43,800.
- Places 쿼터(`places_daily_caps`, 지표 `places.googleapis.com/<이름>`, 한도 `/d/project`): cap = hub 하나의 `GOOGLE_PHOTOS_DAILY_MEDIA_CAP`, H = 운영 키를 쓰는 hub 수(운영 + 시험 hub, 시험 hub 도 운영 키를 쓴다). GetPhotoMediaRequest = 2 × cap × H, SearchTextRequest = GetPlaceRequest = 사진 상한(GetPhotoMediaRequest)의 2배, 코드가 부르지 않는 SearchNearbyRequest·AutocompletePlacesRequest·SearchMediaRequest·SearchReviewPostsRequest = 0.
- Gemini 쿼터(시험 상태, `gemini_daily_caps` 모델 -> 하루 한도): 지표 `generativelanguage.googleapis.com/generate_requests_per_model_per_day`(결제 연결된 1등급), 차원 model, 한도 `/d/model/project`. 한도 이름은 `gcloud auth print-access-token | sed 's/^/Authorization: Bearer /' | curl -s -H @- "https://serviceusage.googleapis.com/v1beta1/projects/gen-lang-client-0035497524/services/generativelanguage.googleapis.com/consumerQuotaMetrics"`(토큰은 표준 입력으로만 넘겨 명령 인자에 남지 않게 한다) 의 `consumerQuotaLimits[].name` 에서 `/limits/` 뒤를 URL 디코딩한 값이다. 단위(`1/d/{project}/{model}`)와 차원 순서가 달라 단위에서 이름을 만들 수 없다. 유료 2·3 등급으로 오르면 등급별 지표(`generate_content_paid_tier_2_requests` 등)가 따로 걸리므로 그때 지표를 다시 정한다.
- `_Required` 예외: `_Required` 싱크·버킷(400일, 잠김)은 global 에 고정된다. 위치는 조직·폴더 기본 설정으로만 바꿀 수 있는데 mapcenter-b59ca 는 조직이 없어 서울로 옮길 수 없다. 관리 활동·시스템 이벤트 감사 로그는 global `_Required` 에 남고, 나머지는 `_Default` -> map-general(서울, 30일)로 간다.
- 접근 기록 정본(고지문 대조표용): IAP 데이터 접근 감사 로그, OS Login 의 로그인 판정(oslogin.googleapis.com 데이터 접근 감사 `CheckPolicy` — 로그인마다 사용자 계정이 남는다, 운영 VM 실측), VM 저널의 ssh·sudo·logind(`map_journald`)이며, 모두 access-audit 싱크로 map-access-audit(서울, 400일)에만 쌓인다(`_Default` 에서는 제외). VM 의 OS Login 모듈이 약 분당 1회 부르는 `ListLoginProfiles` 감사 로그는 사람의 접근이 아니어서 `_Default` 에서 버린다. Ops Agent 의 자체 점검 로그(`ops-agent-health`)도 끄는 설정이 없어 `_Default` 에서 버린다. Google 게스트 에이전트가 Ops Agent 를 거치지 않고 바로 쓰는 자기 로그(`GCEGuestAgent`·`diagnostic-log`)도 같은 이유로 버린다(VM 저널에는 남는다).
- 수동 스냅숏: 프로젝트 기본 위치가 서울이어도 위치를 명시한다. `gcloud compute snapshots create NAME --project=mapcenter-b59ca --source-disk=map-prod-data --source-disk-zone=asia-northeast3-a --storage-location=asia-northeast3`
- 시험 deployer SA 는 시험 VM 인스턴스 단위의 IAP 터널·조회 권한만 갖고 키가 없다. 배포 SSH 는 VM 안 로컬 계정 mapdeploy 의 키(authorized_keys)로 인증해 OS Login 을 거치지 않으므로 OS Login 역할이 필요 없다.
