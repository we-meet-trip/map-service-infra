terraform {
  # 잠금 파일이 OpenTofu 레지스트리 주소로 기록되므로 OpenTofu 로만 실행한다.
  required_version = ">= 1.12.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.5"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 8.5"
    }
  }
}
