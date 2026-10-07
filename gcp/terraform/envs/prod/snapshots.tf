# 프로젝트 기본 스냅숏 위치를 서울 하나로 고정한다(생성은 기존 설정 갱신, 삭제는 상태에서만 빠진다).
resource "google_compute_snapshot_settings" "this" {
  storage_location {
    policy = "SPECIFIC_LOCATIONS"
    locations {
      name     = local.region
      location = local.region
    }
  }
}

resource "google_compute_resource_policy" "daily_snapshot" {
  name   = "map-prod-daily-snapshot"
  region = local.region

  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        # UTC 16:00 = KST 01:00. 4시간 경계이자 정시라 공급자 문서와 GCE 문서의 시작 시각 규칙을 모두 만족한다.
        start_time = "16:00"
      }
    }
    # 원본 디스크를 지우거나 갈아 끼워도 그 디스크의 자동 스냅숏에 7일 보존을 그대로 적용한다(KEEP_AUTO_SNAPSHOTS 면 무기한 남는다).
    retention_policy {
      max_retention_days    = 7
      on_source_disk_delete = "APPLY_RETENTION_POLICY"
    }
    snapshot_properties {
      storage_locations = [local.region]
    }
  }
}

# 디스크 쪽 resource_policies 인수는 beta 전용이라 GA 부착 자원으로 데이터·부팅 디스크에 건다.
resource "google_compute_disk_resource_policy_attachment" "daily_snapshot" {
  for_each = {
    data = module.host.data_disk
    boot = module.host.boot_disk
  }

  name = google_compute_resource_policy.daily_snapshot.name
  disk = each.value
  zone = local.zone
}
