locals {
  vm_sa_member = "serviceAccount:${module.host.vm_sa_email}"
  owner_admin  = { "roles/storage.admin" = ["user:${var.owner_email}"] }
}

module "backups" {
  source = "../../modules/bucket"

  name     = "map-test-backups"
  location = "US-CENTRAL1"
  bindings = merge(local.owner_admin, {
    "roles/storage.objectCreator" = [local.vm_sa_member]
    "roles/storage.objectViewer"  = [local.vm_sa_member]
  })
}

module "artifacts" {
  source = "../../modules/bucket"

  name     = "map-test-artifacts"
  location = "US-CENTRAL1"
  bindings = merge(local.owner_admin, {
    "roles/storage.objectViewer" = [local.vm_sa_member]
  })
}
