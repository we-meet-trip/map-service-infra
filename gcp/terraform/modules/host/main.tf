# 두 환경 공통 호스트: API, 사용자 VPC, IAP SSH, 고정 IP, 전용 SA, VM, 알림 채널, 업타임 체크·경보, 대시보드.

terraform {
  required_providers {
    google = {
      source = "hashicorp/google"
    }
  }
}

locals {
  name = "map-${var.env}"

  # 전용 SA·WIF 를 만들려면 iam API 가 켜져 있어야 한다(새 프로젝트 기본 목록에 없다).
  services = concat([
    "compute.googleapis.com",
    "oslogin.googleapis.com",
    "iap.googleapis.com",
    "secretmanager.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "storage.googleapis.com",
    "serviceusage.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
  ], var.extra_services)

  vm_filter = "resource.type=\"gce_instance\" AND resource.label.instance_id=\"${google_compute_instance.this.instance_id}\""

  # 대시보드와 운영 경보가 같은 필터를 쓴다. 디스크는 늘 100% 인 loop 장치(squashfs·snap)를 뺀다.
  # Monitoring 필터는 NOT 을 그룹 정의에서만 받으므로 제외는 != 오른쪽의 정규식 함수로 쓴다.
  metric_filters = {
    cpu    = "metric.type=\"compute.googleapis.com/instance/cpu/utilization\" AND ${local.vm_filter}"
    memory = "metric.type=\"agent.googleapis.com/memory/percent_used\" AND metric.label.state=\"used\" AND ${local.vm_filter}"
    disk   = "metric.type=\"agent.googleapis.com/disk/percent_used\" AND metric.label.state=\"used\" AND metric.label.device!=monitoring.regex.full_match(\"(/dev/)?loop.*\") AND ${local.vm_filter}"
  }

  uptime_filter = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\""
}

data "google_project" "this" {
  project_id = var.project_id
}

resource "google_project_service" "this" {
  for_each = toset(local.services)

  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "vpc" {
  name                    = "${local.name}-vpc"
  auto_create_subnetworks = false
  # 도커 브리지 MTU(1500)와 맞춘다. GCE 기본 1460 과 어긋나면 큰 외부 응답이 멈출 수 있다.
  mtu = 1500

  depends_on = [google_project_service.this]
}

resource "google_compute_subnetwork" "this" {
  name          = "${local.name}-subnet"
  region        = var.region
  network       = google_compute_network.vpc.id
  ip_cidr_range = var.subnet_cidr
}

# IAP TCP 전달의 출발 대역에서만 22번을 연다.
resource "google_compute_firewall" "iap_ssh" {
  name          = "${local.name}-iap-ssh"
  network       = google_compute_network.vpc.id
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [local.name]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# DNS A 레코드가 가리키는 주소다. 한 번 풀리면 같은 IP 를 다시 예약할 수 없어 상태에 삭제 방지를 남긴다.
resource "google_compute_address" "this" {
  name            = "${local.name}-ip"
  region          = var.region
  address_type    = "EXTERNAL"
  network_tier    = "STANDARD"
  deletion_policy = "PREVENT"

  depends_on = [google_project_service.this]
}

# 범위(scope)는 cloud-platform 으로 열어 두고 실제 권한은 IAM 바인딩으로만 준다.
resource "google_service_account" "vm" {
  account_id   = "${local.name}-vm"
  display_name = "${local.name} VM"

  depends_on = [google_project_service.this]
}

# 포맷하지 않은 빈 디스크로 붙인다. 호스트 등록 도구가 /dev/disk/by-id/google-<device_name> 으로 찾는다.
resource "google_compute_disk" "data" {
  count = var.data_disk_gb > 0 ? 1 : 0

  name            = "${local.name}-data"
  zone            = var.zone
  type            = "pd-balanced"
  size            = var.data_disk_gb
  deletion_policy = "PREVENT"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_instance" "this" {
  name         = local.name
  machine_type = var.machine_type
  zone         = var.zone
  tags         = [local.name]
  # 운영 VM 은 다시 시작하면 데이터 디스크를 LUKS 로 직접 열어야 하므로, 정지가 필요한 변경은 apply 가 실패하게 둔다.
  allow_stopping_for_update = var.env != "prod"
  deletion_protection       = var.env == "prod"

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = var.boot_disk_gb
      type  = "pd-balanced"
    }
  }

  dynamic "attached_disk" {
    for_each = google_compute_disk.data
    content {
      source      = attached_disk.value.self_link
      device_name = attached_disk.value.name
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.this.id

    access_config {
      nat_ip       = google_compute_address.this.address
      network_tier = "STANDARD"
    }
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  scheduling {
    on_host_maintenance = "MIGRATE"
    automatic_restart   = true
  }

  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    serial-port-enable     = "FALSE"
  }

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }
}

resource "google_monitoring_notification_channel" "email" {
  count = length(var.alert_emails)

  display_name = "${local.name} email ${count.index + 1}"
  type         = "email"
  labels = {
    email_address = var.alert_emails[count.index]
  }

  depends_on = [google_project_service.this]
}

resource "google_monitoring_uptime_check_config" "this" {
  for_each = var.uptime_checks

  display_name = "${local.name} ${each.key}"
  timeout      = "10s"
  period       = each.value.period
  # USA(체커 3) + ASIA_PACIFIC + EUROPE = 체커 5.
  selected_regions = ["USA", "ASIA_PACIFIC", "EUROPE"]
  checker_type     = "STATIC_IP_CHECKERS"

  http_check {
    path         = each.value.path
    port         = 443
    use_ssl      = true
    validate_ssl = true

    dynamic "accepted_response_status_codes" {
      for_each = each.value.status == null ? [] : [each.value.status]
      content {
        status_value = accepted_response_status_codes.value
      }
    }
  }

  dynamic "content_matchers" {
    for_each = each.value.content == null ? [] : [each.value.content]
    content {
      content = content_matchers.value
      matcher = "CONTAINS_STRING"
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = each.value.host
    }
  }

  depends_on = [google_project_service.this]
}

# 체크마다 2개 이상의 체커 위치가 5분 동안 실패하면 연다.
resource "google_monitoring_alert_policy" "uptime" {
  display_name          = "${local.name} uptime"
  combiner              = "OR"
  enabled               = true
  notification_channels = google_monitoring_notification_channel.email[*].id

  conditions {
    display_name = "check_passed false at 2+ locations for 5m"
    condition_threshold {
      filter          = local.uptime_filter
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"
      aggregations {
        alignment_period     = "1200s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["metric.label.check_id"]
      }
      trigger {
        count = 1
      }
    }
  }
}

# API 가 돌려주는 정규형(columns 는 문자열, targetAxis 기본값 명시)으로 써서 plan 마다 대시보드가 바뀐 것으로 나오지 않게 한다.
resource "google_monitoring_dashboard" "this" {
  dashboard_json = jsonencode({
    displayName = local.name
    gridLayout = {
      columns = "2"
      widgets = [
        for title, chart in {
          "Uptime check passed" = { filter = local.uptime_filter, aligner = "ALIGN_FRACTION_TRUE" }
          "CPU utilization"     = { filter = local.metric_filters.cpu, aligner = "ALIGN_MEAN" }
          "Memory used %"       = { filter = local.metric_filters.memory, aligner = "ALIGN_MEAN" }
          "Disk used %"         = { filter = local.metric_filters.disk, aligner = "ALIGN_MEAN" }
          } : {
          title = title
          xyChart = {
            dataSets = [{
              plotType   = "LINE"
              targetAxis = "Y1"
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter      = chart.filter
                  aggregation = { alignmentPeriod = "300s", perSeriesAligner = chart.aligner }
                }
              }
            }]
          }
        }
      ]
    }
  })
}
