# 값은 map-test-tf-config 버킷의 test.tfvars 에만 둔다(저장소에는 test.tfvars.example).
# 비공개 값(메일·그룹·결제 계정·예산 금액)은 sensitive 라 plan 출력에 값 대신 (sensitive value) 로 나온다.

variable "alert_emails" {
  type      = list(string)
  sensitive = true
}

# apply 주체(소유자) 이메일. 버킷 권한 표에서 roles/storage.admin 을 받는 유일한 사람이다.
variable "owner_email" {
  type      = string
  sensitive = true
}

# 비어 있으면 팀 그룹 바인딩을 만들지 않는다.
variable "team_group" {
  type      = string
  default   = ""
  sensitive = true
}

variable "billing_account_id" {
  type      = string
  sensitive = true
}

# 예산 금액(원, 결제 계정 통화 KRW). 경보 기준일 뿐 지출 상한이 아니다.
variable "budget_test_krw" {
  type      = number
  sensitive = true
}

# Gemini 모델 이름 -> 하루 요청 한도(시험·개발 Gemini 프로젝트). 정해지기 전에는 비워 둔다.
variable "gemini_daily_caps" {
  type    = map(number)
  default = {}
}

# generate_requests_per_model_per_day 한도 단위에서 '1/'·중괄호를 뺀 값. gemini_daily_caps 를 채울 때 함께 적는다.
variable "gemini_quota_limit" {
  type    = string
  default = null
}

# scripts/gcp_secrets.py 가 만든 시험 런타임 비밀 ID. 시험 VM SA 가 비밀 단위로 읽기만 한다.
variable "runtime_secret_ids" {
  type    = list(string)
  default = []
}
