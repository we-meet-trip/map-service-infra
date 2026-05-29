-- map-service-infra / postgres 초기화 스크립트.
-- postgres 컨테이너 entrypoint(`/docker-entrypoint-initdb.d`)가 첫 부팅 시 1회 실행한다.
-- 멱등 — CREATE IF NOT EXISTS 사용으로 volume 재사용 시 안전.
-- POSTGRES_USER 환경변수가 변경되어도 동작하도록 current_user를 동적으로 사용한다.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS user_service;
CREATE SCHEMA IF NOT EXISTS hub_data;
CREATE SCHEMA IF NOT EXISTS langgraph;

DO $$
BEGIN
  EXECUTE format(
    'ALTER ROLE %I SET search_path = user_service, hub_data, public',
    current_user
  );
END
$$;
