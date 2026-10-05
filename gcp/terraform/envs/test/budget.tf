# 경보만 보내고 지출을 막지는 않는다. 금액은 결제 계정 통화(KRW) 단위다.
resource "google_billing_budget" "test" {
  provider = google.billing

  billing_account = var.billing_account_id
  display_name    = "map mapservice-test"

  budget_filter {
    projects        = ["projects/${module.host.project_number}"]
    calendar_period = "MONTH"
  }

  amount {
    specified_amount {
      currency_code = "KRW"
      units         = tostring(var.budget_test_krw)
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
    monitoring_notification_channels = module.host.notification_channels
  }
}
