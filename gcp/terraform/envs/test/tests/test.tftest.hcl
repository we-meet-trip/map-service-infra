# 자격 증명 없이 시험 구성의 plan 을 확인한다(mock 공급자). API 쪽 거절(통화·권한)은 여기서 잡히지 않는다.

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "111111111111"
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
      name  = "projects/mapservice-test/serviceAccounts/map-test-vm@mapservice-test.iam.gserviceaccount.com"
      email = "map-test-vm@mapservice-test.iam.gserviceaccount.com"
    }
  }
}

mock_provider "google" {
  alias = "billing"
}

mock_provider "google-beta" {}

run "defaults" {
  command = plan

  override_resource {
    target = google_service_account.deployer
    values = {
      name  = "projects/mapservice-test/serviceAccounts/map-test-deployer@mapservice-test.iam.gserviceaccount.com"
      email = "map-test-deployer@mapservice-test.iam.gserviceaccount.com"
    }
  }

  variables {
    alert_emails       = ["owner@example.com", "ops@example.com"]
    owner_email        = "owner@example.com"
    billing_account_id = "000000-000000-000000"
    budget_test_krw    = 50
  }

  assert {
    condition     = google_iam_workload_identity_pool.github.workload_identity_pool_id == "github" && google_iam_workload_identity_pool_provider.github_infra.workload_identity_pool_provider_id == "github-infra" && google_iam_workload_identity_pool_provider.github_infra.oidc[0].issuer_uri == "https://token.actions.githubusercontent.com"
    error_message = "WIF pool github / provider github-infra on the GitHub issuer"
  }
  assert {
    condition     = google_iam_workload_identity_pool_provider.github_infra.attribute_condition == "assertion.repository_owner_id=='272100089' && assertion.repository_id=='1236439535' && assertion.ref=='refs/heads/develop' && assertion.workflow_ref.startsWith('we-meet-trip/map-service-infra/.github/workflows/deploy.yml@') && assertion.environment=='gcp-test' && assertion.event_name in ['workflow_dispatch','workflow_run']"
    error_message = "WIF attribute condition is a fixed contract"
  }
  assert {
    condition = google_iam_workload_identity_pool_provider.github_infra.attribute_mapping == tomap({
      "google.subject"          = "assertion.sub"
      "attribute.repository_id" = "assertion.repository_id"
      "attribute.ref"           = "assertion.ref"
      "attribute.environment"   = "assertion.environment"
    })
    error_message = "WIF attribute mapping"
  }
  assert {
    condition     = google_service_account_iam_member.deployer_wif.role == "roles/iam.workloadIdentityUser" && google_service_account_iam_member.deployer_wif.member == "principalSet://iam.googleapis.com/projects/111111111111/locations/global/workloadIdentityPools/github/attribute.repository_id/1236439535"
    error_message = "only the infra repository may impersonate the deployer"
  }
  assert {
    condition     = google_iap_tunnel_instance_iam_member.deployer.role == "roles/iap.tunnelResourceAccessor" && google_iap_tunnel_instance_iam_member.deployer.instance == "map-test" && google_compute_instance_iam_member.deployer.role == "roles/compute.viewer" && google_compute_instance_iam_member.deployer.instance_name == "map-test" && google_compute_instance_iam_member.deployer.member == "serviceAccount:map-test-deployer@mapservice-test.iam.gserviceaccount.com"
    error_message = "deployer gets instance-level IAP tunnel and viewer only"
  }
  assert {
    condition     = google_compute_firewall.web.name == "map-test-web" && google_compute_firewall.web.source_ranges == toset(["0.0.0.0/0"]) && google_compute_firewall.web.disabled != true
    error_message = "test 80/443 are public"
  }
  assert {
    condition     = google_billing_budget.test.budget_filter[0].projects == toset(["projects/111111111111"]) && length(google_billing_budget.test.threshold_rules) == 4 && google_billing_budget.test.amount[0].specified_amount[0].currency_code == "KRW"
    error_message = "one KRW budget for mapservice-test"
  }
  assert {
    condition     = length(google_service_usage_consumer_quota_override.gemini) == 0 && length(google_secret_manager_secret_iam_member.vm_runtime) == 0 && length(google_project_iam_member.team) == 0 && length(google_service_account_iam_member.team) == 0
    error_message = "no quota, secret or team bindings until values are given"
  }
  assert {
    condition     = toset([for binding in google_project_iam_member.vm_sa : binding.role]) == toset(["roles/logging.logWriter", "roles/monitoring.metricWriter"])
    error_message = "test VM SA project roles"
  }
  assert {
    condition     = length(module.host.uptime_check_ids) == 1 && module.host.data_disk == null && module.host.vm_tag == "map-test"
    error_message = "one uptime check and no data disk"
  }
  assert {
    condition     = output.buckets == { backups = "map-test-backups", artifacts = "map-test-artifacts" }
    error_message = "test bucket names"
  }
  assert {
    condition     = alltrue([for value in [var.alert_emails, var.owner_email, var.team_group, var.billing_account_id, var.budget_test_krw] : issensitive(value)])
    error_message = "private values must stay out of plan output"
  }
}

run "values_add_quota_secret_and_team_bindings" {
  command = plan

  variables {
    alert_emails       = ["owner@example.com"]
    owner_email        = "owner@example.com"
    billing_account_id = "000000-000000-000000"
    budget_test_krw    = 50
    team_group         = "team@example.com"
    runtime_secret_ids = ["map-test-example"]
    gemini_daily_caps  = { "gemini-3.5-flash-lite" = 1000 }
  }

  assert {
    condition     = google_service_usage_consumer_quota_override.gemini["gemini-3.5-flash-lite"].project == "gen-lang-client-0035497524" && google_service_usage_consumer_quota_override.gemini["gemini-3.5-flash-lite"].metric == "generativelanguage.googleapis.com%2Fgenerate_requests_per_model_per_day" && google_service_usage_consumer_quota_override.gemini["gemini-3.5-flash-lite"].limit == "%2Fd%2Fmodel%2Fproject" && google_service_usage_consumer_quota_override.gemini["gemini-3.5-flash-lite"].dimensions == tomap({ model = "gemini-3.5-flash-lite" })
    error_message = "Gemini per-model quota override"
  }
  assert {
    condition     = google_secret_manager_secret_iam_member.vm_runtime["map-test-example"].role == "roles/secretmanager.secretAccessor" && google_secret_manager_secret_iam_member.vm_runtime["map-test-example"].member == "serviceAccount:map-test-vm@mapservice-test.iam.gserviceaccount.com"
    error_message = "secret-level accessor for the test VM SA"
  }
  assert {
    condition     = toset([for binding in google_project_iam_member.team : binding.role]) == toset(["roles/viewer", "roles/logging.viewer", "roles/monitoring.viewer", "roles/compute.osLogin", "roles/iap.tunnelResourceAccessor"]) && google_service_account_iam_member.team[0].role == "roles/iam.serviceAccountUser"
    error_message = "team group roles"
  }
}

run "host_module_test_vm" {
  command = plan

  module {
    source = "../../modules/host"
  }

  variables {
    env            = "test"
    project_id     = "mapservice-test"
    region         = "us-central1"
    zone           = "us-central1-a"
    subnet_cidr    = "10.20.0.0/24"
    machine_type   = "e2-medium"
    boot_disk_gb   = 50
    extra_services = ["iamcredentials.googleapis.com", "sts.googleapis.com"]
    alert_emails   = ["owner@example.com", "ops@example.com"]
    uptime_checks  = { api-healthz-app = { host = "test-api.example.com", path = "/healthz/app", period = "300s", content = "UP" } }
  }

  assert {
    condition     = length(google_compute_disk.data) == 0 && length(google_compute_instance.this.attached_disk) == 0 && google_compute_instance.this.deletion_protection == false && google_compute_instance.this.allow_stopping_for_update
    error_message = "test VM has no data disk, no delete protection and may be stopped for updates"
  }
  assert {
    condition     = google_compute_instance.this.metadata["enable-oslogin"] == "TRUE" && google_compute_instance.this.shielded_instance_config[0].enable_secure_boot && google_compute_instance.this.network_interface[0].access_config[0].network_tier == "STANDARD"
    error_message = "test VM shares the OS Login, Shielded VM and Standard tier settings"
  }
  assert {
    condition     = alltrue([for service in ["iam.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com", "billingbudgets.googleapis.com"] : contains(keys(google_project_service.this), service)])
    error_message = "test API list"
  }
  assert {
    condition     = google_monitoring_alert_policy.uptime.display_name == "map-test uptime" && google_monitoring_alert_policy.uptime.enabled && one(google_monitoring_uptime_check_config.this["api-healthz-app"].content_matchers).content == "UP" && google_monitoring_uptime_check_config.this["api-healthz-app"].period == "300s"
    error_message = "map-test uptime check and alert"
  }
}
