# 운영 버킷 권한 표에는 apply 주체(소유자)와 VM SA 만 남는다. VM SA 에 프로젝트 단위 storage 역할은 주지 않는다.
locals {
  vm_sa_member = "serviceAccount:${module.host.vm_sa_email}"
  owner_admin  = { "roles/storage.admin" = ["user:${var.owner_email}"] }

  # 버킷마다 권한 표 전체. VM SA 는 백업을 올리고 읽기만 하며(지우는 것은 수명 규칙뿐) 산출물은 읽기만 한다.
  bucket_bindings = {
    backups = merge(local.owner_admin, {
      "roles/storage.objectCreator" = [local.vm_sa_member]
      "roles/storage.objectViewer"  = [local.vm_sa_member]
    })
    artifacts = merge(local.owner_admin, {
      "roles/storage.objectViewer" = [local.vm_sa_member]
    })
    archive = local.owner_admin
  }

  # 보관소의 최상위 접두어 -> 삭제 나이(일). 보존 기간이 다른 기록은 서로 다른 최상위 접두어에 둔다(matchesPrefix 는 앞부분 일치라
  # 짧은 규칙의 접두어 아래에 둔 긴 기록도 짧은 쪽으로 지워진다). incident/ 의 사고 증거에는 규칙을 두지 않는다.
  archive_delete_after_days = {
    "log-1y/"  = 366
    "log-6m/"  = 184
    "perm-5y/" = 1827
  }

  # VM SA 는 보관소에 서비스 요청 기록만 올린다. 이 접두어에 새 객체를 만드는 것만 되고 읽기·덮어쓰기·삭제는 안 된다.
  archive_conditional_bindings = [{
    role       = "roles/storage.objectCreator"
    members    = [local.vm_sa_member]
    title      = "request-log-upload-only"
    expression = "resource.type == \"storage.googleapis.com/Object\" && resource.name.startsWith(\"projects/_/buckets/map-prod-archive/objects/log-6m/\")"
  }]
}

module "backups" {
  source = "../../modules/bucket"

  name                = "map-prod-backups"
  location            = "ASIA-NORTHEAST3"
  soft_delete_seconds = 0
  delete_after_days   = { "" = local.backup_retention_days }
  bindings            = local.bucket_bindings.backups
}

module "artifacts" {
  source = "../../modules/bucket"

  name     = "map-prod-release-artifacts"
  location = "ASIA-NORTHEAST3"
  bindings = local.bucket_bindings.artifacts
}

module "archive" {
  source = "../../modules/bucket"

  name                 = "map-prod-archive"
  location             = "ASIA-NORTHEAST3"
  soft_delete_seconds  = 0
  delete_after_days    = local.archive_delete_after_days
  bindings             = local.bucket_bindings.archive
  conditional_bindings = local.archive_conditional_bindings
}
