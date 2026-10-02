output "instance_id" {
  value = module.host.instance_id
}

output "zone" {
  value = module.host.zone
}

output "external_ip" {
  value = module.host.external_ip
}

output "vm_sa" {
  value = module.host.vm_sa_email
}

output "deployer_sa" {
  value = google_service_account.deployer.email
}

# deploy.yml 의 workload_identity_provider 에 넣는 전체 리소스 이름.
output "wif_provider" {
  value = google_iam_workload_identity_pool_provider.github_infra.name
}

output "buckets" {
  value = {
    backups   = module.backups.name
    artifacts = module.artifacts.name
  }
}
