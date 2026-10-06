# 버킷 하나와 그 권한 표 전체. 모든 버킷은 균일 접근·공개 차단·삭제 방지다.

terraform {
  required_providers {
    google = {
      source = "hashicorp/google"
    }
  }
}

variable "name" {
  type = string
}

variable "location" {
  type = string
}

# 역할 -> 구성원. 이 표와 conditional_bindings 가 버킷 권한의 전부라 둘에 없는 바인딩(projectEditor·projectViewer 편의 바인딩 포함)은 지워진다.
variable "bindings" {
  type = map(list(string))

  # 기본 역할 owner 는 버킷 조회·정책 조회 권한이 본래 없어서, 편의 바인딩이 사라지면 apply 주체가 다음 refresh 에서 403 을 받는다.
  validation {
    condition     = length(lookup(var.bindings, "roles/storage.admin", [])) > 0
    error_message = "bindings 에 apply 주체의 roles/storage.admin 이 있어야 한다."
  }
}

# 조건이 붙은 바인딩. 객체 이름 접두어로 범위를 좁힐 때 쓴다(버킷 균일 접근이 켜져 있어야 조건을 받는다).
variable "conditional_bindings" {
  type = list(object({
    role       = string
    members    = list(string)
    title      = string
    expression = string
  }))
  default = []
}

# 객체 접두어 -> 삭제 나이(일). 키 "" 는 버킷 전체.
variable "delete_after_days" {
  type    = map(number)
  default = {}

  # 나이가 없거나 0 인 Delete 규칙은 그 접두어의 객체를 모두 지운다.
  validation {
    condition     = alltrue([for days in values(var.delete_after_days) : coalesce(days, 0) >= 1])
    error_message = "delete_after_days 의 값은 1 이상의 일수여야 한다."
  }
}

# null 이면 GCS 기본 soft delete 를 그대로 둔다. 0 은 끈다.
variable "soft_delete_seconds" {
  type    = number
  default = null
}

resource "google_storage_bucket" "this" {
  name                        = var.name
  location                    = var.location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  # 상태에 남아 설정 블록이 지워져도 삭제를 막는다(prevent_destroy 는 블록과 함께 사라진다).
  deletion_policy = "PREVENT"

  versioning {
    enabled = false
  }

  dynamic "soft_delete_policy" {
    for_each = var.soft_delete_seconds == null ? [] : [var.soft_delete_seconds]
    content {
      retention_duration_seconds = soft_delete_policy.value
    }
  }

  dynamic "lifecycle_rule" {
    for_each = var.delete_after_days
    content {
      condition {
        age            = lifecycle_rule.value
        matches_prefix = lifecycle_rule.key == "" ? null : [lifecycle_rule.key]
      }
      action {
        type = "Delete"
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

data "google_iam_policy" "this" {
  dynamic "binding" {
    for_each = var.bindings
    content {
      role    = binding.key
      members = binding.value
    }
  }
  dynamic "binding" {
    for_each = var.conditional_bindings
    content {
      role    = binding.value.role
      members = binding.value.members
      condition {
        title      = binding.value.title
        expression = binding.value.expression
      }
    }
  }
}

resource "google_storage_bucket_iam_policy" "this" {
  bucket      = google_storage_bucket.this.name
  policy_data = data.google_iam_policy.this.policy_data
}

output "name" {
  value = google_storage_bucket.this.name
}
