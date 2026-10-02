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

# content 는 응답 본문에 있어야 할 문자열, status 는 2xx 대신 받아들일 응답 코드(로그인 없는 users/me 는 401 이 정상).
locals {
  uptime_checks = {
    api-healthz-app = { host = "api.mapservice.app", path = "/healthz/app", period = "60s", content = "UP" }
    api-healthz     = { host = "api.mapservice.app", path = "/healthz", period = "60s" }
    api-users-me    = { host = "api.mapservice.app", path = "/api/v1/users/me", period = "300s", status = 401 }
    web-app-config  = { host = "mapservice.app", path = "/app_config.json", period = "300s", content = "api_base_url" }
    web-aasa        = { host = "mapservice.app", path = "/.well-known/apple-app-site-association", period = "300s", content = "applinks" }
    web-privacy     = { host = "mapservice.app", path = "/legal/privacy.html", period = "300s" }
  }
}

module "host" {
  source = "../../modules/host"

  env           = "prod"
  project_id    = local.project_id
  region        = local.region
  zone          = local.zone
  subnet_cidr   = "10.10.0.0/24"
  machine_type  = "e2-standard-2"
  boot_disk_gb  = 40
  data_disk_gb  = 100
  alert_emails  = var.alert_emails
  uptime_checks = local.uptime_checks
}

resource "google_compute_firewall" "web_owner" {
  name          = "map-prod-web-owner"
  network       = module.host.network_id
  direction     = "INGRESS"
  source_ranges = var.owner_cidrs
  target_tags   = [module.host.vm_tag]

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}

# 기본은 꺼짐. 전환 창에서 gcloud 로 켜며, 그 뒤 기본값을 true 로 바꾸기 전까지 이 상태로 apply 하면 운영 전체가 멈춘다.
resource "google_compute_firewall" "web_public" {
  name          = "map-prod-web-public"
  network       = module.host.network_id
  direction     = "INGRESS"
  source_ranges = ["0.0.0.0/0"]
  target_tags   = [module.host.vm_tag]
  disabled      = !var.prod_public_web

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}
