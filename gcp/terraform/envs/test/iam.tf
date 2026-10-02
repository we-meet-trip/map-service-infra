# 프로젝트 단위로 owner·editor·SA 토큰·키·사용자·compute 관리·storage.admin·secretmanager 역할을 주지 않는다.

resource "google_project_iam_member" "vm_sa" {
  for_each = toset(["roles/logging.logWriter", "roles/monitoring.metricWriter"])

  project = local.project_id
  role    = each.value
  member  = local.vm_sa_member
}

# Terraform 은 비밀을 만들지 않는다. 이미 있는 비밀마다 시험 VM SA 에 읽기만 준다.
resource "google_secret_manager_secret_iam_member" "vm_runtime" {
  for_each = toset(var.runtime_secret_ids)

  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = local.vm_sa_member
}

# 그룹 주소는 sensitive 라 비었는지만 풀어 자원 수를 정한다.
resource "google_project_iam_member" "team" {
  for_each = toset(nonsensitive(var.team_group == "") ? [] : [
    "roles/viewer",
    "roles/logging.viewer",
    "roles/monitoring.viewer",
    "roles/compute.osLogin",
    "roles/iap.tunnelResourceAccessor",
  ])

  project = local.project_id
  role    = each.value
  member  = "group:${var.team_group}"
}

# VM 이 전용 SA 로 돌기 때문에 OS Login 사용자는 그 SA 에 대한 serviceAccountUser 가 있어야 한다(SA 단위로만).
resource "google_service_account_iam_member" "team" {
  count = var.team_group == "" ? 0 : 1

  service_account_id = module.host.vm_sa_id
  role               = "roles/iam.serviceAccountUser"
  member             = "group:${var.team_group}"
}
