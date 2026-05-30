-- =========================================================================
-- map-service-infra / postgres 초기화 스크립트
--
-- postgres 컨테이너 entrypoint(`/docker-entrypoint-initdb.d`)가 첫 부팅 시
-- 1회 실행한다. 멱등(idempotent) — CREATE IF NOT EXISTS 만 사용하므로
-- 볼륨이 재사용되거나 스크립트가 재실행되어도 안전하다.
-- POSTGRES_USER 환경변수가 변경되어도 동작하도록 current_user 를
-- 동적으로 사용한다(특정 사용자명에 하드코딩하지 않음).
-- =========================================================================

-- PostGIS 확장 활성화.
-- 공간(geo) 좌표·거리 계산에 사용되는 GIS 기능을 데이터베이스에 등록한다.
-- 이미 활성화된 경우 IF NOT EXISTS 로 건너뛴다.
CREATE EXTENSION IF NOT EXISTS postgis;

-- 도메인별 스키마 분리.
--   user_service : 사용자/계정/즐겨찾기 등 user 서비스 영역(Spring 측 BFF 소유)
--   hub_data     : 외부 데이터(날씨/지역 등) 캐시 영역(hub 서비스 소유)
--   langgraph    : LangGraph 체크포인트/상태 저장 영역(agent 서비스 소유)
-- 각 서비스는 자기 스키마 외부의 테이블에 직접 쓰기 접근하지 않는 것이 원칙.
CREATE SCHEMA IF NOT EXISTS user_service;
CREATE SCHEMA IF NOT EXISTS hub_data;
CREATE SCHEMA IF NOT EXISTS langgraph;

-- 현재 로그인 ROLE 의 기본 search_path 를 변경한다.
-- DO $$ ... $$ 블록 + format(%I) 를 사용하는 이유:
--   ALTER ROLE 은 식별자(ROLE 이름)를 파라미터로 받을 수 없기 때문에
--   current_user 를 동적 SQL 로 안전하게 인용(%I)하여 주입한다.
-- 결과: 본 ROLE 로 접속한 세션은 테이블을 user_service → hub_data → public
-- 순서로 자동 검색하므로, 평소 쿼리에서 스키마 접두사를 생략해도 동작한다.
DO $$
BEGIN
  EXECUTE format(
    'ALTER ROLE %I SET search_path = user_service, hub_data, public',
    current_user
  );
END
$$;
