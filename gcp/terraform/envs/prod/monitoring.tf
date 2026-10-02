# 업타임 체크·"map-prod uptime" 경보·대시보드는 modules/host 에 있다.

locals {
  channels = module.host.notification_channels

  # 로그 일치 경보는 조건이 하나뿐이라 OR 를 필터 안에 둔다. 로그 일치 경보는 프로젝트 싱크가 로그 버킷으로 보낸 항목을 검사하므로,
  # system_event(_Required)와 map_journald(access-audit 싱크 -> map-access-audit)는 _Default 제외와 무관하게 대상이다.
  vm_restart_filter = join(" ", [
    "resource.type=\"gce_instance\" AND resource.labels.instance_id=\"${module.host.instance_id}\" AND (",
    "(logName=\"projects/${local.project_id}/logs/cloudaudit.googleapis.com%2Fsystem_event\" AND protoPayload.methodName=(\"compute.instances.hostError\" OR \"compute.instances.automaticRestart\" OR \"compute.instances.guestTerminate\" OR \"compute.instances.preempted\"))",
    "OR (logName=\"${local.journald_log_name}\" AND jsonPayload._SYSTEMD_UNIT=\"systemd-logind.service\" AND jsonPayload.MESSAGE:\"System is rebooting\")",
    ")",
  ])
}

resource "google_monitoring_alert_policy" "ssl_expiry" {
  display_name          = "map-prod ssl expiry"
  combiner              = "OR"
  enabled               = true
  notification_channels = local.channels

  conditions {
    display_name = "certificate expires in < 14 days"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/time_until_ssl_cert_expires\" AND resource.type=\"uptime_url\""
      comparison      = "COMPARISON_LT"
      threshold_value = 14
      duration        = "600s"
      aggregations {
        alignment_period     = "1200s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_MIN"
        group_by_fields      = ["metric.label.check_id"]
      }
      trigger {
        count = 1
      }
    }
  }
}

resource "google_monitoring_alert_policy" "vm_restart" {
  display_name          = "map-prod vm restart"
  combiner              = "OR"
  enabled               = true
  notification_channels = local.channels

  conditions {
    display_name = "host event or guest reboot"
    condition_matched_log {
      filter = local.vm_restart_filter
    }
  }

  # 로그 일치 조건에는 notification_rate_limit 이 필수다.
  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = "LUKS 수동 해제 필요. map-prod 가 다시 시작됐다. 데이터 디스크(map-prod-data)는 재부팅 뒤 운영자가 LUKS 를 직접 열고 검증된 마운트를 복구해야 한다. 절차: map-service-infra 저장소 docs/NCP_PRODUCTION_SERVING.md 의 'Stop, reboot and backup acceptance'(해제 뒤 Docker 와 서비스를 올리고 PG ID·마운트·상태·공개/비공개 상태를 기록)."
  }
}

resource "google_monitoring_alert_policy" "cpu" {
  display_name          = "map-prod cpu"
  combiner              = "OR"
  enabled               = var.prod_vm_alerts
  notification_channels = local.channels

  conditions {
    display_name = "cpu utilization > 85% for 15m"
    condition_threshold {
      filter          = module.host.metric_filters.cpu
      comparison      = "COMPARISON_GT"
      threshold_value = 0.85
      duration        = "900s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MEAN"
      }
      trigger {
        count = 1
      }
    }
  }
}

resource "google_monitoring_alert_policy" "memory" {
  display_name          = "map-prod memory"
  combiner              = "OR"
  enabled               = var.prod_vm_alerts
  notification_channels = local.channels

  conditions {
    display_name = "memory used > 90% for 10m"
    condition_threshold {
      filter          = module.host.metric_filters.memory
      comparison      = "COMPARISON_GT"
      threshold_value = 90
      duration        = "600s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
      trigger {
        count = 1
      }
    }
  }
}

# 장치별 시계열이 따로 평가되므로 어느 실제 장치든 80% 를 넘으면 열린다.
resource "google_monitoring_alert_policy" "disk" {
  display_name          = "map-prod disk"
  combiner              = "OR"
  enabled               = var.prod_vm_alerts
  notification_channels = local.channels

  conditions {
    display_name = "disk used > 80%"
    condition_threshold {
      filter          = module.host.metric_filters.disk
      comparison      = "COMPARISON_GT"
      threshold_value = 80
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MEAN"
      }
      trigger {
        count = 1
      }
    }
  }
}

# 사용자 로그 지표는 제외 필터와 무관하게 받은 로그 전부를 센다.
resource "google_logging_metric" "backup_complete" {
  name   = "map_backup_complete"
  filter = "logName=\"${local.journald_log_name}\" AND jsonPayload.MESSAGE=~\"^MAP_BACKUP_RESULT=COMPLETE\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key        = "kind"
      value_type = "STRING"
    }
  }

  label_extractors = {
    kind = "REGEXP_EXTRACT(jsonPayload.MESSAGE, \"kind=([a-z]+)\")"
  }
}

# 실행기가 남긴 실패 줄과, 유닛이 실패로 끝났을 때 systemd(PID 1)가 남기는 'Failed with result' 줄을 함께 센다.
resource "google_logging_metric" "backup_failed" {
  name   = "map_backup_failed"
  filter = "logName=\"${local.journald_log_name}\" AND (jsonPayload.MESSAGE=~\"^MAP_BACKUP_RESULT=FAILED\" OR (jsonPayload.UNIT=~\"^map-prod-.*backup.*[.]service$\" AND jsonPayload.MESSAGE:\"Failed with result\"))"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

# 부재 조건은 지표가 한 번은 들어와야 성립한다. 백업 종류(kind)마다 따로 본다.
resource "google_monitoring_alert_policy" "backup_absent" {
  display_name          = "map-prod backup success absent"
  combiner              = "OR"
  enabled               = var.prod_vm_alerts
  notification_channels = local.channels

  conditions {
    display_name = "no successful backup for 75m"
    condition_absent {
      filter   = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.backup_complete.name}\" AND resource.type=\"gce_instance\""
      duration = "4500s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.kind"]
      }
      trigger {
        count = 1
      }
    }
  }
}

resource "google_monitoring_alert_policy" "backup_failed" {
  display_name          = "map-prod backup failed"
  combiner              = "OR"
  enabled               = var.prod_vm_alerts
  notification_channels = local.channels

  conditions {
    display_name = "backup failure count > 0"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.backup_failed.name}\" AND resource.type=\"gce_instance\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger {
        count = 1
      }
    }
  }
}
