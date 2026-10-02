# 프로젝트 단위로 owner·editor·SA 토큰·키·사용자·compute 관리·storage.admin·secretmanager 역할을 주지 않는다.
# 운영 VM SA 에는 비밀 바인딩이 없다.

resource "google_project_iam_member" "vm_sa" {
  for_each = toset(["roles/logging.logWriter", "roles/monitoring.metricWriter"])

  project = local.project_id
  role    = each.value
  member  = local.vm_sa_member
}

# 운영자는 개인 계정으로 OS Login(관리자)·IAP 터널을 쓴다. 자원 키는 비공개가 아닌 별칭만 풀어 쓰고, 주소는 sensitive 로 남는다.
resource "google_project_iam_member" "operators" {
  for_each = {
    for pair in setproduct(nonsensitive(keys(var.operators)), ["roles/compute.osAdminLogin", "roles/iap.tunnelResourceAccessor"]) :
    "${pair[0]} ${pair[1]}" => { alias = pair[0], role = pair[1] }
  }

  project = local.project_id
  role    = each.value.role
  member  = "user:${var.operators[each.value.alias]}"
}

# VM 이 전용 SA 로 돌기 때문에 OS Login 사용자는 그 SA 에 대한 serviceAccountUser 가 있어야 한다(SA 단위로만).
resource "google_service_account_iam_member" "operators" {
  for_each = toset(nonsensitive(keys(var.operators)))

  service_account_id = module.host.vm_sa_id
  role               = "roles/iam.serviceAccountUser"
  member             = "user:${var.operators[each.key]}"
}

# 그룹 주소는 sensitive 라 비었는지만 풀어 자원 수를 정한다.
resource "google_project_iam_member" "team" {
  for_each = toset(nonsensitive(var.team_group == "") ? [] : ["roles/logging.viewer", "roles/monitoring.viewer"])

  project = local.project_id
  role    = each.value
  member  = "group:${var.team_group}"
}
