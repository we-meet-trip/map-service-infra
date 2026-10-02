# Places API(New) 의 하루·프로젝트 단위 한도(단위 1/d/{project}). google-beta 전용 자원이며 limit 은 '1/'·중괄호를 뺀 꼴로 적는다.
# 값이 정해지기 전에는 places_daily_caps 가 비어 자원이 없다. 계산식은 README.
resource "google_service_usage_consumer_quota_override" "places" {
  provider = google-beta
  for_each = var.places_daily_caps

  project        = local.project_id
  service        = "places.googleapis.com"
  metric         = urlencode("places.googleapis.com/${each.key}")
  limit          = urlencode("/d/project")
  override_value = tostring(each.value)
  # 기존 한도보다 10% 넘게 낮추는 재정의도 받아들인다.
  force = true
}
