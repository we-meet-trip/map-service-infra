output "project_number" {
  value = data.google_project.this.number
}

output "network_id" {
  value = google_compute_network.vpc.id
}

# VM 에 붙은 네트워크 태그. 환경별 방화벽 규칙이 이 태그를 대상으로 한다.
output "vm_tag" {
  value = local.name
}

output "instance_id" {
  value = google_compute_instance.this.instance_id
}

output "instance_name" {
  value = google_compute_instance.this.name
}

output "zone" {
  value = google_compute_instance.this.zone
}

output "external_ip" {
  value = google_compute_address.this.address
}

output "vm_sa_email" {
  value = google_service_account.vm.email
}

# SA 단위 IAM 의 service_account_id 로 쓰는 전체 이름.
output "vm_sa_id" {
  value = google_service_account.vm.name
}

output "boot_disk" {
  value = basename(google_compute_instance.this.boot_disk[0].source)
}

output "data_disk" {
  value = one(google_compute_disk.data[*].name)
}

output "notification_channels" {
  value = google_monitoring_notification_channel.email[*].id
}

output "metric_filters" {
  value = local.metric_filters
}

output "uptime_check_ids" {
  value = { for name, check in google_monitoring_uptime_check_config.this : name => check.uptime_check_id }
}

output "uptime_alert_policy" {
  value = google_monitoring_alert_policy.uptime.name
}
