output "instance_id" {
  value = module.host.instance_id
}

output "zone" {
  value = module.host.zone
}

output "prod_external_ip" {
  value = module.host.external_ip
}

output "vm_sa" {
  value = module.host.vm_sa_email
}

output "backup_bucket" {
  value = module.backups.name
}

output "artifacts_bucket" {
  value = module.artifacts.name
}

output "archive_bucket" {
  value = module.archive.name
}

output "archive_lifecycle_applied" {
  value = var.archive_retention_days != null
}

output "log_buckets" {
  value = {
    access_audit = google_logging_project_bucket_config.access_audit.name
    general      = google_logging_project_bucket_config.general.name
  }
}

output "journald_log_name" {
  value = local.journald_log_name
}

output "web_public_firewall" {
  value = google_compute_firewall.web_public.name
}

# gcloud 로 켜고 끌 때 쓰는 정책 리소스 이름.
output "vm_alert_policies" {
  value = [
    google_monitoring_alert_policy.cpu.name,
    google_monitoring_alert_policy.memory.name,
    google_monitoring_alert_policy.disk.name,
  ]
}

output "backup_alert_policies" {
  value = [
    google_monitoring_alert_policy.backup_absent.name,
    google_monitoring_alert_policy.backup_failed.name,
  ]
}

output "always_on_alert_policies" {
  value = [
    module.host.uptime_alert_policy,
    google_monitoring_alert_policy.ssl_expiry.name,
    google_monitoring_alert_policy.vm_restart.name,
  ]
}

output "uptime_check_ids" {
  value = module.host.uptime_check_ids
}

output "admin_tunnel_command" {
  value = "gcloud compute ssh map-prod --project=mapcenter-b59ca --zone=asia-northeast3-a --tunnel-through-iap -- -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8003:127.0.0.1:8003"
}
