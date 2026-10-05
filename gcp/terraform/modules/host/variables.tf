variable "env" {
  type = string

  validation {
    condition     = contains(["prod", "test"], var.env)
    error_message = "env 는 prod 또는 test 다."
  }
}

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "zone" {
  type = string
}

variable "subnet_cidr" {
  type = string
}

variable "machine_type" {
  type = string
}

variable "boot_disk_gb" {
  type = number
}

# 0 이면 데이터 디스크를 만들지 않는다.
variable "data_disk_gb" {
  type    = number
  default = 0
}

# 두 환경 공통 API 목록 뒤에 붙일 이 환경 전용 API.
variable "extra_services" {
  type    = list(string)
  default = []
}

variable "alert_emails" {
  type = list(string)

  validation {
    condition     = length(var.alert_emails) > 0
    error_message = "경보를 받을 이메일이 하나 이상 있어야 한다."
  }
}

# 이름 -> 업타임 체크. content 는 응답 본문에 있어야 할 문자열, status 는 2xx 대신 받아들일 응답 코드.
variable "uptime_checks" {
  type = map(object({
    host    = string
    path    = string
    period  = string
    content = optional(string)
    status  = optional(number)
  }))
}
