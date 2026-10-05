locals {
  project_id = "mapservice-test"
  region     = "us-central1"
  zone       = "us-central1-a"
}

provider "google" {
  project = local.project_id
  region  = local.region
  zone    = local.zone
}

# 예산 API 는 사용자 ADC 로 부를 때 할당량 프로젝트를 명시하지 않으면 403 을 낸다.
provider "google" {
  alias                 = "billing"
  billing_project       = local.project_id
  user_project_override = true
}

provider "google-beta" {
  project = local.project_id
  region  = local.region
}

# 시험 VM 은 상시 가동이라 시작·정지 일정을 두지 않는다. 업타임 체크·"map-test uptime" 경보·대시보드도 이 모듈이 만든다.
module "host" {
  source = "../../modules/host"

  env            = "test"
  project_id     = local.project_id
  region         = local.region
  zone           = local.zone
  subnet_cidr    = "10.20.0.0/24"
  machine_type   = "e2-medium"
  boot_disk_gb   = 50
  extra_services = ["iamcredentials.googleapis.com", "sts.googleapis.com"]
  alert_emails   = var.alert_emails

  uptime_checks = {
    api-healthz-app = { host = "test-api.mapservice.app", path = "/healthz/app", period = "300s", content = "UP" }
  }
}

resource "google_compute_firewall" "web" {
  name          = "map-test-web"
  network       = module.host.network_id
  direction     = "INGRESS"
  source_ranges = ["0.0.0.0/0"]
  target_tags   = [module.host.vm_tag]

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}
