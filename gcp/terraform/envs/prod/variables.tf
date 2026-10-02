# 값은 map-prod-tf-config 버킷의 prod.tfvars 에만 둔다(저장소에는 prod.tfvars.example).
# 비공개 값(메일·소유자 IP·운영자·그룹·결제 계정·예산 금액)은 sensitive 라 plan 출력에 값 대신 (sensitive value) 로 나온다.

variable "alert_emails" {
  type      = list(string)
  sensitive = true
}

# web-owner 규칙의 출발지(소유자 /32 목록).
variable "owner_cidrs" {
  type      = list(string)
  sensitive = true

  validation {
    condition     = length(var.owner_cidrs) > 0 && alltrue([for cidr in var.owner_cidrs : endswith(cidr, "/32")])
    error_message = "owner_cidrs 는 /32 주소 목록이어야 한다."
  }
}

# apply 주체(소유자) 이메일. 버킷 권한 표에서 roles/storage.admin 을 받는 유일한 사람이다.
variable "owner_email" {
  type      = string
  sensitive = true
}

# 별칭 -> 이메일. 운영 VM 에 OS Login(관리자)·IAP 로 들어가는 사람.
variable "operators" {
  type      = map(string)
  sensitive = true
}

# 비어 있으면 팀 그룹 바인딩을 만들지 않는다.
variable "team_group" {
  type      = string
  default   = ""
  sensitive = true
}

# false 면 web-public 규칙이 꺼진 채로 남는다. 운영 전환 뒤 기본값을 true 로 바꾸기 전에는 README 의 적용 금지 구간을 따른다.
variable "prod_public_web" {
  type    = bool
  default = false
}

# CPU·메모리·디스크·백업 경보를 켤지. 첫 백업 성공 지표를 확인한 뒤 켠다.
variable "prod_vm_alerts" {
  type    = bool
  default = false
}

variable "billing_account_id" {
  type      = string
  sensitive = true
}

# 예산 금액(원, 결제 계정 통화 KRW). 경보 기준일 뿐 지출 상한이 아니다.
variable "budget_account_krw" {
  type      = number
  sensitive = true
}

variable "budget_project_krw" {
  type      = number
  sensitive = true
}

variable "budget_gemini_krw" {
  type      = number
  sensitive = true
}

# Places API(New) 지표 이름 -> 하루 한도. 비어 있으면 재정의를 만들지 않는다.
variable "places_daily_caps" {
  type    = map(number)
  default = {}

  validation {
    condition = alltrue([for metric in keys(var.places_daily_caps) : contains([
      "AutocompletePlacesRequest",
      "GetPhotoMediaRequest",
      "GetPlaceRequest",
      "SearchMediaRequest",
      "SearchNearbyRequest",
      "SearchReviewPostsRequest",
      "SearchTextRequest",
    ], metric)])
    error_message = "places_daily_caps 의 키는 places.googleapis.com 의 하루·프로젝트 단위 지표 이름이어야 한다."
  }
}

variable "access_audit_retention_days" {
  type    = number
  default = 400
}

# map-prod-archive 의 접두어별 보존 일수. 정해지기 전(null)에는 수명 규칙을 만들지 않는다.
variable "archive_retention_days" {
  type = object({
    dump       = number
    access_log = number
  })
  default = null

  # 나이가 없거나 0 인 Delete 규칙은 그 접두어의 객체를 모두 지운다.
  validation {
    condition     = var.archive_retention_days == null ? true : alltrue([for days in values(var.archive_retention_days) : coalesce(days, 0) >= 1])
    error_message = "archive_retention_days 는 null(규칙 없음)이거나 두 값 모두 1 이상의 일수여야 한다."
  }
}
