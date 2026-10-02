# 자격 증명 없이 운영 구성의 plan 을 확인한다(mock 공급자). API 쪽 거절(필터 문법·통화·권한)은 여기서 잡히지 않는다.

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "483544691563"
    }
  }
  mock_data "google_iam_policy" {
    defaults = {
      policy_data = "{\"bindings\":[]}"
    }
  }
  # 공급자가 SA 이름 형식을 정규식으로 검사하므로 실제 꼴의 값을 준다.
  mock_resource "google_service_account" {
    defaults = {
      name  = "projects/mapcenter-b59ca/serviceAccounts/map-prod-vm@mapcenter-b59ca.iam.gserviceaccount.com"
      email = "map-prod-vm@mapcenter-b59ca.iam.gserviceaccount.com"
    }
  }
  mock_resource "google_compute_instance" {
    defaults = {
      instance_id = "1234567890123456789"
    }
  }
}

mock_provider "google" {
  alias = "billing"
}

mock_provider "google-beta" {}

run "defaults_keep_public_web_and_vm_alerts_off" {
  command = plan

  variables {
    alert_emails       = ["owner@example.com", "ops@example.com"]
    owner_cidrs        = ["203.0.113.10/32"]
    owner_email        = "owner@example.com"
    operators          = { owner = "owner@example.com", ops = "ops@example.com" }
    billing_account_id = "000000-000000-000000"
    budget_account_krw = 200
    budget_project_krw = 140
    budget_gemini_krw  = 20
  }

  assert {
    condition     = google_compute_firewall.web_public.disabled == true && google_compute_firewall.web_public.source_ranges == toset(["0.0.0.0/0"])
    error_message = "web-public must default to disabled"
  }
  assert {
    condition     = google_compute_firewall.web_owner.disabled != true && google_compute_firewall.web_owner.source_ranges == toset(["203.0.113.10/32"])
    error_message = "web-owner must stay enabled for the owner /32 list"
  }
  assert {
    condition = alltrue([for policy in [
      google_monitoring_alert_policy.cpu,
      google_monitoring_alert_policy.memory,
      google_monitoring_alert_policy.disk,
      google_monitoring_alert_policy.backup_absent,
      google_monitoring_alert_policy.backup_failed,
    ] : policy.enabled == false])
    error_message = "VM and backup alerts must default to disabled"
  }
  assert {
    condition     = google_monitoring_alert_policy.ssl_expiry.enabled && google_monitoring_alert_policy.vm_restart.enabled
    error_message = "ssl expiry and vm restart alerts must always be enabled"
  }
  assert {
    condition = [
      google_monitoring_alert_policy.ssl_expiry.display_name,
      google_monitoring_alert_policy.vm_restart.display_name,
      google_monitoring_alert_policy.cpu.display_name,
      google_monitoring_alert_policy.memory.display_name,
      google_monitoring_alert_policy.disk.display_name,
      google_monitoring_alert_policy.backup_absent.display_name,
      google_monitoring_alert_policy.backup_failed.display_name,
      ] == [
      "map-prod ssl expiry",
      "map-prod vm restart",
      "map-prod cpu",
      "map-prod memory",
      "map-prod disk",
      "map-prod backup success absent",
      "map-prod backup failed",
    ]
    error_message = "alert policy display names are a fixed contract"
  }
  assert {
    condition     = google_monitoring_alert_policy.vm_restart.alert_strategy[0].notification_rate_limit[0].period == "300s" && strcontains(google_monitoring_alert_policy.vm_restart.documentation[0].content, "LUKS 수동 해제 필요") && strcontains(google_monitoring_alert_policy.vm_restart.documentation[0].content, "docs/NCP_PRODUCTION_SERVING.md")
    error_message = "log-match alert needs a rate limit and the LUKS runbook pointer"
  }
  assert {
    condition     = length(google_monitoring_alert_policy.vm_restart.conditions) == 1 && google_monitoring_alert_policy.vm_restart.conditions[0].condition_matched_log[0].filter == "resource.type=\"gce_instance\" AND resource.labels.instance_id=\"1234567890123456789\" AND ( (logName=\"projects/mapcenter-b59ca/logs/cloudaudit.googleapis.com%2Fsystem_event\" AND protoPayload.methodName=(\"compute.instances.hostError\" OR \"compute.instances.automaticRestart\" OR \"compute.instances.guestTerminate\" OR \"compute.instances.preempted\")) OR (logName=\"projects/mapcenter-b59ca/logs/map_journald\" AND jsonPayload._SYSTEMD_UNIT=\"systemd-logind.service\" AND jsonPayload.MESSAGE:\"System is rebooting\") )"
    error_message = "vm restart filter: system_event methods OR logind reboot line, this instance only"
  }
  assert {
    condition = strcontains(google_monitoring_alert_policy.disk.conditions[0].condition_threshold[0].filter, "metric.label.device!=monitoring.regex.full_match(\"(/dev/)?loop.*\")") && alltrue([for filter in concat(values(module.host.metric_filters), [
      google_monitoring_alert_policy.ssl_expiry.conditions[0].condition_threshold[0].filter,
      google_monitoring_alert_policy.backup_failed.conditions[0].condition_threshold[0].filter,
      google_monitoring_alert_policy.backup_absent.conditions[0].condition_absent[0].filter,
    ]) : !strcontains(filter, "NOT ")])
    error_message = "disk alert must skip loop devices with != (Monitoring filters accept NOT only in group definitions)"
  }
  assert {
    condition = [
      for policy in [google_monitoring_alert_policy.ssl_expiry, google_monitoring_alert_policy.cpu, google_monitoring_alert_policy.memory, google_monitoring_alert_policy.disk] :
      "${policy.conditions[0].condition_threshold[0].comparison} ${policy.conditions[0].condition_threshold[0].threshold_value} ${policy.conditions[0].condition_threshold[0].duration}"
      ] == [
      "COMPARISON_LT 14 600s",
      "COMPARISON_GT 0.85 900s",
      "COMPARISON_GT 90 600s",
      "COMPARISON_GT 80 300s",
    ]
    error_message = "thresholds: ssl < 14 days, cpu > 0.85 for 15m, memory > 90% for 10m, disk > 80%"
  }
  assert {
    condition = local.uptime_checks == {
      api-healthz-app = { host = "api.mapservice.app", path = "/healthz/app", period = "60s", content = "UP" }
      api-healthz     = { host = "api.mapservice.app", path = "/healthz", period = "60s" }
      api-users-me    = { host = "api.mapservice.app", path = "/api/v1/users/me", period = "300s", status = 401 }
      web-app-config  = { host = "mapservice.app", path = "/app_config.json", period = "300s", content = "api_base_url" }
      web-aasa        = { host = "mapservice.app", path = "/.well-known/apple-app-site-association", period = "300s", content = "applinks" }
      web-privacy     = { host = "mapservice.app", path = "/legal/privacy.html", period = "300s" }
    }
    error_message = "production uptime targets, periods, body strings and the users/me 401"
  }
  assert {
    condition = local.bucket_bindings == {
      backups = {
        "roles/storage.admin"         = ["user:owner@example.com"]
        "roles/storage.objectCreator" = ["serviceAccount:map-prod-vm@mapcenter-b59ca.iam.gserviceaccount.com"]
        "roles/storage.objectViewer"  = ["serviceAccount:map-prod-vm@mapcenter-b59ca.iam.gserviceaccount.com"]
      }
      artifacts = {
        "roles/storage.admin"        = ["user:owner@example.com"]
        "roles/storage.objectViewer" = ["serviceAccount:map-prod-vm@mapcenter-b59ca.iam.gserviceaccount.com"]
      }
      archive = { "roles/storage.admin" = ["user:owner@example.com"] }
    }
    error_message = "bucket permission tables: owner storage.admin; VM SA may create and read backups and read artifacts, nothing else"
  }
  assert {
    condition     = alltrue([for value in [var.alert_emails, var.owner_cidrs, var.owner_email, var.operators, var.team_group, var.billing_account_id, var.budget_account_krw, var.budget_project_krw, var.budget_gemini_krw] : issensitive(value)])
    error_message = "private values must stay out of plan output"
  }
  assert {
    condition     = google_monitoring_alert_policy.backup_absent.conditions[0].condition_absent[0].duration == "4500s"
    error_message = "backup success absence window must be 75 minutes"
  }
  assert {
    condition     = google_logging_metric.backup_complete.filter == "logName=\"projects/mapcenter-b59ca/logs/map_journald\" AND jsonPayload.MESSAGE=~\"^MAP_BACKUP_RESULT=COMPLETE\""
    error_message = "backup success metric filter"
  }
  assert {
    condition     = strcontains(google_logging_metric.backup_failed.filter, "jsonPayload.MESSAGE=~\"^MAP_BACKUP_RESULT=FAILED\"") && strcontains(google_logging_metric.backup_failed.filter, "jsonPayload.MESSAGE:\"Failed with result\"")
    error_message = "backup failure metric must count result lines and systemd unit failures"
  }
  assert {
    condition     = local.backup_retention_days == 7
    error_message = "backup retention is 7 days"
  }
  assert {
    condition     = output.archive_lifecycle_applied == false && output.backup_bucket == "map-prod-backups" && output.artifacts_bucket == "map-prod-release-artifacts" && output.archive_bucket == "map-prod-archive"
    error_message = "bucket names and archive lifecycle default"
  }
  assert {
    condition     = google_logging_project_sink.default.filter == "NOT LOG_ID(\"cloudaudit.googleapis.com/activity\") AND NOT LOG_ID(\"externalaudit.googleapis.com/activity\") AND NOT LOG_ID(\"cloudaudit.googleapis.com/system_event\") AND NOT LOG_ID(\"externalaudit.googleapis.com/system_event\") AND NOT LOG_ID(\"cloudaudit.googleapis.com/access_transparency\") AND NOT LOG_ID(\"externalaudit.googleapis.com/access_transparency\")"
    error_message = "_Default filter must stay the project default verbatim"
  }
  assert {
    condition     = google_logging_project_sink.default.destination == "logging.googleapis.com/projects/mapcenter-b59ca/locations/asia-northeast3/buckets/map-general" && google_logging_project_sink.default.exclusions[0].filter == google_logging_project_sink.access_audit.filter && google_logging_project_sink.default.exclusions[1].filter == "logName=\"projects/mapcenter-b59ca/logs/ops-agent-health\""
    error_message = "_Default must route to map-general and exclude the access-audit and Ops Agent health logs"
  }
  assert {
    condition     = google_logging_project_sink.access_audit.filter == "(logName=\"projects/mapcenter-b59ca/logs/cloudaudit.googleapis.com%2Fdata_access\" AND protoPayload.serviceName=(\"iap.googleapis.com\" OR \"secretmanager.googleapis.com\")) OR logName=\"projects/mapcenter-b59ca/logs/map_journald\""
    error_message = "access-audit sink filter"
  }
  assert {
    condition     = google_logging_project_sink.access_audit.destination == "logging.googleapis.com/projects/mapcenter-b59ca/locations/asia-northeast3/buckets/map-access-audit" && google_logging_project_bucket_config.access_audit.retention_days == 400 && google_logging_project_bucket_config.general.retention_days == 30
    error_message = "log buckets and retention"
  }
  assert {
    condition     = keys(google_project_iam_audit_config.access) == ["iap.googleapis.com", "secretmanager.googleapis.com"] && alltrue([for config in google_project_iam_audit_config.access : toset([for log in config.audit_log_config : log.log_type]) == toset(["ADMIN_READ", "DATA_READ"])])
    error_message = "IAP and Secret Manager read audit logs"
  }
  assert {
    condition     = google_compute_snapshot_settings.this.storage_location[0].policy == "SPECIFIC_LOCATIONS" && [for location in google_compute_snapshot_settings.this.storage_location[0].locations : location.location] == ["asia-northeast3"]
    error_message = "snapshot settings must pin Seoul"
  }
  assert {
    condition     = google_compute_resource_policy.daily_snapshot.snapshot_schedule_policy[0].schedule[0].daily_schedule[0].start_time == "16:00" && google_compute_resource_policy.daily_snapshot.snapshot_schedule_policy[0].retention_policy[0].max_retention_days == 7 && google_compute_resource_policy.daily_snapshot.snapshot_schedule_policy[0].retention_policy[0].on_source_disk_delete == "KEEP_AUTO_SNAPSHOTS" && google_compute_resource_policy.daily_snapshot.snapshot_schedule_policy[0].snapshot_properties[0].storage_locations == toset(["asia-northeast3"])
    error_message = "daily snapshot schedule"
  }
  assert {
    condition     = keys(google_compute_disk_resource_policy_attachment.daily_snapshot) == ["boot", "data"] && google_compute_disk_resource_policy_attachment.daily_snapshot["data"].disk == "map-prod-data"
    error_message = "snapshot schedule must cover the data and boot disks"
  }
  assert {
    condition     = length(google_billing_budget.this) == 3 && google_billing_budget.this["mapcenter"].budget_filter[0].projects == toset(["projects/483544691563"]) && google_billing_budget.this["gemini"].budget_filter[0].projects == toset(["projects/198634605164"])
    error_message = "budgets: billing account, mapcenter-b59ca, Gemini"
  }
  assert {
    condition     = [for rule in google_billing_budget.this["account"].threshold_rules : "${rule.threshold_percent}:${coalesce(rule.spend_basis, "CURRENT_SPEND")}"] == ["0.5:CURRENT_SPEND", "0.9:CURRENT_SPEND", "1:CURRENT_SPEND", "1:FORECASTED_SPEND"] && google_billing_budget.this["account"].amount[0].specified_amount[0].currency_code == "KRW"
    error_message = "budget thresholds and currency"
  }
  assert {
    condition     = length(google_service_usage_consumer_quota_override.places) == 0
    error_message = "no Places quota override until the cap is decided"
  }
  assert {
    condition = alltrue([for binding in concat(values(google_project_iam_member.vm_sa), values(google_project_iam_member.operators), values(google_project_iam_member.team)) : !contains([
      "roles/owner", "roles/editor", "roles/iam.serviceAccountTokenCreator", "roles/iam.serviceAccountKeyAdmin", "roles/iam.serviceAccountUser",
      "roles/compute.admin", "roles/storage.admin",
    ], binding.role) && !startswith(binding.role, "roles/compute.instanceAdmin") && !startswith(binding.role, "roles/secretmanager.")])
    error_message = "forbidden project-level role"
  }
  assert {
    condition     = toset([for binding in google_project_iam_member.operators : binding.role]) == toset(["roles/compute.osAdminLogin", "roles/iap.tunnelResourceAccessor"]) && length(google_project_iam_member.operators) == 4 && length(google_service_account_iam_member.operators) == 2 && length(google_project_iam_member.team) == 0
    error_message = "operators get OS Login admin, IAP and VM SA user; no team group by default"
  }
  assert {
    condition     = length(module.host.uptime_check_ids) == 6 && module.host.data_disk == "map-prod-data" && module.host.vm_tag == "map-prod"
    error_message = "six uptime checks and the fixed data disk name"
  }
  assert {
    condition     = output.admin_tunnel_command == "gcloud compute ssh map-prod --project=mapcenter-b59ca --zone=asia-northeast3-a --tunnel-through-iap -- -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8003:127.0.0.1:8003"
    error_message = "admin tunnel command"
  }
}

run "cutover_values_turn_public_web_and_alerts_on" {
  command = plan

  variables {
    alert_emails           = ["owner@example.com"]
    owner_cidrs            = ["203.0.113.10/32"]
    owner_email            = "owner@example.com"
    operators              = { owner = "owner@example.com" }
    billing_account_id     = "000000-000000-000000"
    budget_account_krw     = 200
    budget_project_krw     = 140
    budget_gemini_krw      = 20
    prod_public_web        = true
    prod_vm_alerts         = true
    places_daily_caps      = { GetPhotoMediaRequest = 600 }
    archive_retention_days = { dump = 30, access_log = 400 }
  }

  assert {
    condition     = google_compute_firewall.web_public.disabled == false && google_monitoring_alert_policy.cpu.enabled && google_monitoring_alert_policy.backup_failed.enabled
    error_message = "cutover values must enable web-public and the alerts"
  }
  assert {
    condition     = google_service_usage_consumer_quota_override.places["GetPhotoMediaRequest"].metric == "places.googleapis.com%2FGetPhotoMediaRequest" && google_service_usage_consumer_quota_override.places["GetPhotoMediaRequest"].limit == "%2Fd%2Fproject" && google_service_usage_consumer_quota_override.places["GetPhotoMediaRequest"].override_value == "600"
    error_message = "Places metric and limit must be url-encoded"
  }
  assert {
    condition     = output.archive_lifecycle_applied
    error_message = "archive lifecycle applied once retention is decided"
  }
}

run "rejects_non_host_owner_cidr_unknown_places_metric_and_archive_age_below_one_day" {
  command = plan

  variables {
    alert_emails           = ["owner@example.com"]
    owner_cidrs            = ["203.0.113.0/24"]
    owner_email            = "owner@example.com"
    operators              = {}
    billing_account_id     = "000000-000000-000000"
    budget_account_krw     = 1
    budget_project_krw     = 1
    budget_gemini_krw      = 1
    places_daily_caps      = { FindPlaceRequest = 1 }
    archive_retention_days = { dump = null, access_log = 0 }
  }

  expect_failures = [var.owner_cidrs, var.places_daily_caps, var.archive_retention_days]
}

run "bucket_module_prevents_destroy_and_expires_backups" {
  command = plan

  module {
    source = "../../modules/bucket"
  }

  variables {
    name                = "map-prod-backups"
    location            = "ASIA-NORTHEAST3"
    soft_delete_seconds = 0
    delete_after_days   = { "" = 7, "dump/" = 30 }
    bindings            = { "roles/storage.admin" = ["user:owner@example.com"] }
  }

  assert {
    condition     = google_storage_bucket.this.deletion_policy == "PREVENT" && google_storage_bucket.this.public_access_prevention == "enforced" && google_storage_bucket.this.uniform_bucket_level_access && google_storage_bucket.this.versioning[0].enabled == false
    error_message = "bucket must be delete-protected, private and unversioned"
  }
  assert {
    condition     = google_storage_bucket.this.soft_delete_policy[0].retention_duration_seconds == 0
    error_message = "soft delete off"
  }
  assert {
    condition     = toset([for rule in google_storage_bucket.this.lifecycle_rule : "${one(rule.action).type}:${one(rule.condition).age}:${join(",", coalesce(one(rule.condition).matches_prefix, []))}"]) == toset(["Delete:7:", "Delete:30:dump/"])
    error_message = "whole-bucket and per-prefix delete rules"
  }
}

run "bucket_module_requires_apply_principal_admin_and_a_delete_age" {
  command = plan

  module {
    source = "../../modules/bucket"
  }

  variables {
    name              = "map-prod-archive"
    location          = "ASIA-NORTHEAST3"
    delete_after_days = { "access-log/" = null }
    bindings          = { "roles/storage.objectViewer" = ["user:owner@example.com"] }
  }

  expect_failures = [var.bindings, var.delete_after_days]
}

run "bucket_module_rejects_zero_day_delete_age" {
  command = plan

  module {
    source = "../../modules/bucket"
  }

  variables {
    name              = "map-prod-archive"
    location          = "ASIA-NORTHEAST3"
    delete_after_days = { "dump/" = 0 }
    bindings          = { "roles/storage.admin" = ["user:owner@example.com"] }
  }

  expect_failures = [var.delete_after_days]
}

run "host_module_prod_vm" {
  command = plan

  module {
    source = "../../modules/host"
  }

  variables {
    env          = "prod"
    project_id   = "mapcenter-b59ca"
    region       = "asia-northeast3"
    zone         = "asia-northeast3-a"
    subnet_cidr  = "10.10.0.0/24"
    machine_type = "e2-standard-2"
    boot_disk_gb = 40
    data_disk_gb = 100
    alert_emails = ["owner@example.com", "ops@example.com"]
    uptime_checks = {
      api      = { host = "api.example.com", path = "/healthz/app", period = "60s", content = "UP" }
      users-me = { host = "api.example.com", path = "/api/v1/users/me", period = "300s", status = 401 }
    }
  }

  assert {
    condition     = google_compute_disk.data[0].deletion_policy == "PREVENT" && google_compute_disk.data[0].type == "pd-balanced" && google_compute_instance.this.attached_disk[0].device_name == "map-prod-data"
    error_message = "data disk is delete-protected and attached as map-prod-data"
  }
  assert {
    condition     = google_compute_instance.this.deletion_protection && google_compute_instance.this.shielded_instance_config[0].enable_secure_boot && google_compute_instance.this.shielded_instance_config[0].enable_vtpm && google_compute_instance.this.shielded_instance_config[0].enable_integrity_monitoring
    error_message = "prod VM shielded and delete-protected"
  }
  assert {
    condition     = google_compute_instance.this.allow_stopping_for_update == false && google_compute_address.this.deletion_policy == "PREVENT"
    error_message = "apply must never stop the prod VM (the data disk needs a manual LUKS unlock) and the published address is delete-protected"
  }
  assert {
    condition     = google_monitoring_alert_policy.uptime.conditions[0].condition_threshold[0].comparison == "COMPARISON_GT" && google_monitoring_alert_policy.uptime.conditions[0].condition_threshold[0].threshold_value == 1 && google_monitoring_alert_policy.uptime.conditions[0].condition_threshold[0].duration == "300s" && google_monitoring_alert_policy.uptime.conditions[0].condition_threshold[0].aggregations[0].per_series_aligner == "ALIGN_NEXT_OLDER" && google_monitoring_alert_policy.uptime.conditions[0].condition_threshold[0].aggregations[0].cross_series_reducer == "REDUCE_COUNT_FALSE" && google_monitoring_alert_policy.uptime.conditions[0].condition_threshold[0].aggregations[0].group_by_fields == tolist(["metric.label.check_id"])
    error_message = "uptime alert opens when a check fails at 2+ locations for 5 minutes"
  }
  assert {
    condition     = alltrue([for check in google_monitoring_uptime_check_config.this : check.selected_regions == tolist(["USA", "ASIA_PACIFIC", "EUROPE"]) && check.http_check[0].use_ssl && check.http_check[0].validate_ssl])
    error_message = "every uptime check runs from USA, ASIA_PACIFIC and EUROPE (5 checkers) over validated TLS"
  }
  assert {
    condition     = one(google_monitoring_uptime_check_config.this["users-me"].http_check[0].accepted_response_status_codes).status_value == 401 && length(google_monitoring_uptime_check_config.this["users-me"].content_matchers) == 0 && one(google_monitoring_uptime_check_config.this["api"].content_matchers).content == "UP" && length(google_monitoring_uptime_check_config.this["api"].http_check[0].accepted_response_status_codes) == 0
    error_message = "status and body matchers appear only on the checks that ask for them"
  }
  assert {
    condition     = google_compute_instance.this.metadata == tomap({ "enable-oslogin" = "TRUE", "block-project-ssh-keys" = "TRUE", "serial-port-enable" = "FALSE" }) && google_compute_instance.this.service_account[0].scopes == toset(["cloud-platform"])
    error_message = "OS Login metadata and cloud-platform scope"
  }
  assert {
    condition     = google_compute_instance.this.scheduling[0].on_host_maintenance == "MIGRATE" && google_compute_instance.this.scheduling[0].automatic_restart && google_compute_instance.this.network_interface[0].access_config[0].network_tier == "STANDARD" && google_compute_address.this.network_tier == "STANDARD"
    error_message = "live migration and Standard tier"
  }
  assert {
    condition     = google_compute_network.vpc.mtu == 1500 && google_compute_network.vpc.auto_create_subnetworks == false && google_compute_firewall.iap_ssh.source_ranges == toset(["35.235.240.0/20"]) && google_compute_firewall.iap_ssh.name == "map-prod-iap-ssh"
    error_message = "custom VPC with MTU 1500 and IAP-only SSH"
  }
  assert {
    condition     = contains(keys(google_project_service.this), "iam.googleapis.com") && !contains(keys(google_project_service.this), "sts.googleapis.com") && alltrue([for service in google_project_service.this : service.disable_on_destroy == false])
    error_message = "prod API list"
  }
  assert {
    condition     = length(google_monitoring_notification_channel.email) == 2 && google_monitoring_alert_policy.uptime.display_name == "map-prod uptime" && google_monitoring_alert_policy.uptime.enabled && google_monitoring_uptime_check_config.this["api"].http_check[0].validate_ssl
    error_message = "two email channels and the uptime alert"
  }
}
