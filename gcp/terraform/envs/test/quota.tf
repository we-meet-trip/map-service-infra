# 시험·개발 Gemini 프로젝트의 모델별 하루 요청 한도. google-beta 전용 자원이다.
# limit 은 consumerQuotaMetrics 의 한도 이름(/limits/ 뒤, URL 디코딩)이다. 단위(1/d/{project}/{model})와 차원 순서가 다르다.
# 시험 Places 는 운영 키·운영 프로젝트 청구를 쓰므로 시험 프로젝트에는 Places 한도가 없다.
resource "google_service_usage_consumer_quota_override" "gemini" {
  provider = google-beta
  for_each = var.gemini_daily_caps

  project        = "gen-lang-client-0035497524"
  service        = "generativelanguage.googleapis.com"
  metric         = urlencode("generativelanguage.googleapis.com/generate_requests_per_model_per_day")
  limit          = urlencode("/d/model/project")
  dimensions     = { model = each.key }
  override_value = tostring(each.value)
  # 기존 한도보다 10% 넘게 낮추는 재정의도 받아들인다.
  force = true
}
