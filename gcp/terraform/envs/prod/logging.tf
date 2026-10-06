locals {
  # 프로젝트가 만들어질 때의 _Default 필터 원문. 설정에서 빠지면 다음 plan 이 필터를 지워 감사 로그가 map-general 에도 쌓인다.
  default_sink_filter = join(" AND ", [for log_id in [
    "cloudaudit.googleapis.com/activity",
    "externalaudit.googleapis.com/activity",
    "cloudaudit.googleapis.com/system_event",
    "externalaudit.googleapis.com/system_event",
    "cloudaudit.googleapis.com/access_transparency",
    "externalaudit.googleapis.com/access_transparency",
  ] : "NOT LOG_ID(\"${log_id}\")"])

  # 데이터 접근 감사를 켜는 서비스와 로그 유형. Storage 는 백업·보관소 객체의 생성·삭제까지 접근 기록으로 남긴다.
  access_audit_log_types = {
    "iap.googleapis.com"           = ["ADMIN_READ", "DATA_READ"]
    "secretmanager.googleapis.com" = ["ADMIN_READ", "DATA_READ"]
    "storage.googleapis.com"       = ["ADMIN_READ", "DATA_READ", "DATA_WRITE"]
  }

  # 접근 기록(위 서비스의 데이터 접근 감사, OS Login 의 로그인 판정, VM 저널의 ssh·sudo·logind·백업 결과)은 map-access-audit 에만 남긴다.
  # 서비스 목록을 위 표에서 만들어, 감사를 켠 서비스가 이 싱크와 _Default 제외에서 빠지지 않게 한다.
  access_audit_filter = "(logName=\"projects/${local.project_id}/logs/cloudaudit.googleapis.com%2Fdata_access\" AND protoPayload.serviceName=(${join(" OR ", [for service in keys(local.access_audit_log_types) : "\"${service}\""])})) OR (logName=\"projects/${local.project_id}/logs/cloudaudit.googleapis.com%2Fdata_access\" AND protoPayload.serviceName=\"oslogin.googleapis.com\" AND protoPayload.methodName:\"OsLoginDataPlaneService.CheckPolicy\") OR logName=\"${local.journald_log_name}\""

  # 권한 부여·변경·말소 기록: 모든 자원의 IAM 정책 변경(프로젝트·비밀·서비스 계정·IAP·인스턴스 — methodName 부분 일치는 대소문자를 가리지 않는다),
  # 버킷 IAM 변경, 서비스 계정 키의 생성·등록·삭제·사용 중지·재사용, 커스텀 역할과 거부·접근 경계 정책 변경. OS Login 권한은 역할 부여라 IAM 정책 변경으로 남는다.
  permission_audit_filter = "LOG_ID(\"cloudaudit.googleapis.com/activity\") AND (protoPayload.methodName:\"SetIamPolicy\" OR protoPayload.methodName=\"storage.setIamPermissions\" OR protoPayload.methodName:(\"CreateServiceAccountKey\" OR \"UploadServiceAccountKey\" OR \"DeleteServiceAccountKey\" OR \"DisableServiceAccountKey\" OR \"EnableServiceAccountKey\") OR protoPayload.methodName:(\"google.iam.admin.v1.CreateRole\" OR \"google.iam.admin.v1.UpdateRole\" OR \"google.iam.admin.v1.DeleteRole\" OR \"google.iam.admin.v1.UndeleteRole\" OR \"google.iam.v2\" OR \"google.iam.v3\"))"
}

resource "google_project_iam_audit_config" "access" {
  for_each = local.access_audit_log_types

  project = local.project_id
  service = each.key

  dynamic "audit_log_config" {
    for_each = each.value
    content {
      log_type = audit_log_config.value
    }
  }
}

resource "google_logging_project_bucket_config" "access_audit" {
  project        = local.project_id
  location       = local.region
  bucket_id      = "map-access-audit"
  retention_days = var.access_audit_retention_days
}

resource "google_logging_project_bucket_config" "general" {
  project        = local.project_id
  location       = local.region
  bucket_id      = "map-general"
  retention_days = 30
}

# 권한 부여·변경·말소 내역의 서울 사본(5년). 원본은 global _Required(400일)에 그대로 남는다. 잠그지 않는다(잠금은 되돌릴 수 없다).
resource "google_logging_project_bucket_config" "permission_audit" {
  project        = local.project_id
  location       = local.region
  bucket_id      = "map-permission-audit"
  retention_days = 1830
}

# 같은 프로젝트의 로그 버킷이 목적지면 싱크는 자동으로 쓰기 권한을 얻으므로 작성자 ID 에 IAM 을 주지 않는다.
resource "google_logging_project_sink" "access_audit" {
  name        = "access-audit"
  destination = "logging.googleapis.com/projects/${local.project_id}/locations/${local.region}/buckets/${google_logging_project_bucket_config.access_audit.bucket_id}"
  filter      = local.access_audit_filter
}

# 관리 활동 감사 로그는 _Default 에서 이미 빠지므로 이 싱크와 _Required 에만 들어간다.
resource "google_logging_project_sink" "permission_audit" {
  name        = "permission-audit"
  destination = "logging.googleapis.com/projects/${local.project_id}/locations/${local.region}/buckets/${google_logging_project_bucket_config.permission_audit.bucket_id}"
  filter      = local.permission_audit_filter
}

# 기존 _Default 싱크를 가져오기 없이 넘겨받는다(생성은 기존 싱크 갱신, 삭제는 상태에서만 빠진다).
resource "google_logging_project_sink" "default" {
  name        = "_Default"
  destination = "logging.googleapis.com/projects/${local.project_id}/locations/${local.region}/buckets/${google_logging_project_bucket_config.general.bucket_id}"
  filter      = local.default_sink_filter

  exclusions {
    name   = "access-audit"
    filter = local.access_audit_filter
  }

  # Google 게스트 에이전트가 Ops Agent 를 거치지 않고 Cloud Logging 에 바로 쓰는 자기 운영 로그(재시작·주기 작업). VM 저널에는 그대로 남는다.
  # 이름은 사전순으로 둔다. API 가 목록을 정렬해 돌려줘도 순서 차이가 계획 변경으로 보이지 않게 한다.
  exclusions {
    name   = "guest-agent"
    filter = "logName=(\"projects/${local.project_id}/logs/GCEGuestAgent\" OR \"projects/${local.project_id}/logs/diagnostic-log\")"
  }

  # Ops Agent 가 시작할 때와 런타임 오류 때 남기는 자체 점검 로그. 에이전트에 끄는 설정이 없어 저장 단계에서 버린다.
  exclusions {
    name   = "ops-agent-health"
    filter = "logName=\"projects/${local.project_id}/logs/ops-agent-health\""
  }

  # VM 의 OS Login 모듈이 계정 정보를 조회할 때마다(약 분당 1회) 남는 데이터 접근 감사. 사람의 접근이 아니어서 저장하지 않는다.
  exclusions {
    name   = "oslogin-list-profiles"
    filter = "logName=\"projects/${local.project_id}/logs/cloudaudit.googleapis.com%2Fdata_access\" AND protoPayload.serviceName=\"oslogin.googleapis.com\" AND protoPayload.methodName:\"OsLoginDataPlaneService.ListLoginProfiles\""
  }
}
