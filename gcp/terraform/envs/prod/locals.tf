locals {
  project_id = "mapcenter-b59ca"
  region     = "asia-northeast3"
  zone       = "asia-northeast3-a"

  # Ops Agent journald 수신기(map_journald)가 쓰는 로그 이름.
  journald_log_name = "projects/${local.project_id}/logs/map_journald"
}

locals {
  # map-prod-backups 의 Delete 나이. scripts/gcs_backup_transport.py 의 RETENTION_DAYS 와 같아야 한다(테스트가 두 값을 대조한다).
  backup_retention_days = 7
}
