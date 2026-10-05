locals {
  # 예산 필터는 프로젝트 번호만 받는다. 프로젝트 번호는 바뀌지 않는다.
  gemini_project_number = "198634605164"

  # 결제 계정 전체(필터 없음)와 운영·Gemini 프로젝트 각각.
  budgets = {
    account   = { name = "map billing account", units = var.budget_account_krw, projects = null }
    mapcenter = { name = "map mapcenter-b59ca", units = var.budget_project_krw, projects = ["projects/${module.host.project_number}"] }
    gemini    = { name = "map gen-lang-client-0035497524", units = var.budget_gemini_krw, projects = ["projects/${local.gemini_project_number}"] }
  }
}

# 경보만 보내고 지출을 막지는 않는다. 금액은 결제 계정 통화(KRW) 단위다.
resource "google_billing_budget" "this" {
  for_each = local.budgets
  provider = google.billing

  billing_account = var.billing_account_id
  display_name    = each.value.name

  budget_filter {
    projects        = each.value.projects
    calendar_period = "MONTH"
  }

  amount {
    specified_amount {
      currency_code = "KRW"
      units         = tostring(each.value.units)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  all_updates_rule {
    monitoring_notification_channels = local.channels
  }
}
