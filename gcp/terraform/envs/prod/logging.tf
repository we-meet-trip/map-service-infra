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

  # 접근 기록(IAP·Secret Manager 데이터 접근 감사, VM 저널의 ssh·sudo·logind·백업 결과)은 map-access-audit 에만 남긴다.
  access_audit_filter = "(logName=\"projects/${local.project_id}/logs/cloudaudit.googleapis.com%2Fdata_access\" AND protoPayload.serviceName=(\"iap.googleapis.com\" OR \"secretmanager.googleapis.com\")) OR logName=\"${local.journald_log_name}\""
}

resource "google_project_iam_audit_config" "access" {
  for_each = toset(["iap.googleapis.com", "secretmanager.googleapis.com"])

  project = local.project_id
  service = each.value

  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
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

# 같은 프로젝트의 로그 버킷이 목적지면 싱크는 자동으로 쓰기 권한을 얻으므로 작성자 ID 에 IAM 을 주지 않는다.
resource "google_logging_project_sink" "access_audit" {
  name        = "access-audit"
  destination = "logging.googleapis.com/projects/${local.project_id}/locations/${local.region}/buckets/${google_logging_project_bucket_config.access_audit.bucket_id}"
  filter      = local.access_audit_filter
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
}
