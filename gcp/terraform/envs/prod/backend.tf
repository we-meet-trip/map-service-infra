# 부분 구성: tofu init -backend-config=bucket=map-prod-tfstate -backend-config=prefix=terraform/prod
terraform {
  backend "gcs" {}
}
