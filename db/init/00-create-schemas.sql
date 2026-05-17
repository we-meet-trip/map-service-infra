-- map-service-infra / postgres 초기화 스크립트.
-- postgres 컨테이너 entrypoint(`/docker-entrypoint-initdb.d`)가 첫 부팅 시 1회 실행한다.
-- 멱등 — CREATE IF NOT EXISTS 사용으로 volume 재사용 시 안전.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS user_service AUTHORIZATION map;
CREATE SCHEMA IF NOT EXISTS hub_data     AUTHORIZATION map;
CREATE SCHEMA IF NOT EXISTS langgraph    AUTHORIZATION map;

GRANT ALL ON SCHEMA user_service TO map;
GRANT ALL ON SCHEMA hub_data     TO map;
GRANT ALL ON SCHEMA langgraph    TO map;

ALTER ROLE map SET search_path = user_service, hub_data, public;
