# GitHub Actions(infra 저장소 deploy.yml)가 키 없이 시험 deployer SA 로 들어오는 경로.

locals {
  github_repository_id = "1236439535"
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"

  depends_on = [module.host]
}

# GitHub 발급자는 모든 저장소가 함께 쓰므로 소유자·저장소 숫자 ID, 브랜치, 워크플로, 환경, 이벤트로 좁힌다.
resource "google_iam_workload_identity_pool_provider" "github_infra" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-infra"

  attribute_mapping = {
    "google.subject"          = "assertion.sub"
    "attribute.repository_id" = "assertion.repository_id"
    "attribute.ref"           = "assertion.ref"
    "attribute.environment"   = "assertion.environment"
  }

  attribute_condition = join(" && ", [
    "assertion.repository_owner_id=='272100089'",
    "assertion.repository_id=='${local.github_repository_id}'",
    "assertion.ref=='refs/heads/develop'",
    "assertion.workflow_ref.startsWith('we-meet-trip/map-service-infra/.github/workflows/deploy.yml@')",
    "assertion.environment=='gcp-test'",
    "assertion.event_name in ['workflow_dispatch','workflow_run']",
  ])

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# 키를 만들지 않는다. 배포 SSH 는 VM 안 로컬 계정(mapdeploy)의 키로 하므로 OS Login 역할도 주지 않는다.
resource "google_service_account" "deployer" {
  account_id   = "map-test-deployer"
  display_name = "map-test deployer"

  depends_on = [module.host]
}

resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/projects/${module.host.project_number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.github.workload_identity_pool_id}/attribute.repository_id/${local.github_repository_id}"
}

# deployer 권한은 시험 VM 인스턴스 단위의 IAP 터널과 조회뿐이다.
resource "google_iap_tunnel_instance_iam_member" "deployer" {
  zone     = local.zone
  instance = module.host.instance_name
  role     = "roles/iap.tunnelResourceAccessor"
  member   = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_compute_instance_iam_member" "deployer" {
  zone          = local.zone
  instance_name = module.host.instance_name
  role          = "roles/compute.viewer"
  member        = "serviceAccount:${google_service_account.deployer.email}"
}
