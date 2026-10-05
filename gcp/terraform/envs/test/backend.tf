# 부분 구성: tofu init -backend-config=bucket=map-test-tfstate -backend-config=prefix=terraform/test
terraform {
  backend "gcs" {}
}
