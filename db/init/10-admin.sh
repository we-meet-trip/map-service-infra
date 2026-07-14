#!/bin/bash
# =========================================================================
# map-service-infra / postgres 초기화 — map_admin 읽기전용 역할 (admin 모니터링)
#
# postgres 컨테이너 entrypoint(/docker-entrypoint-initdb.d)가 최초 부팅 시
# 00-create-schemas.sql 다음(사전 순)에 1회 실행한다.
#
# 중요: initdb.d 는 "빈 데이터 볼륨의 최초 부팅"에만 자동 실행된다. 이미
# postgres-data 볼륨이 있는 환경에서는 자동 실행되지 않으므로, 아래를 1회
# 수동 적용해야 한다(멱등이라 반복 안전):
#   docker compose exec postgres bash /docker-entrypoint-initdb.d/10-admin.sh
#
# 비밀번호는 MAP_ADMIN_PASSWORD(컨테이너 env, 공유 .env 유래) 단일 출처에서
# 온다. admin 컨테이너의 ADMIN_DATABASE_URL 비밀번호와 반드시 일치해야 한다.
#
# cut2(2026-07-13): admin_data 스키마 소유권을 map_admin 으로 이전한다.
#   - hub_data 는 계속 SELECT-only(읽기전용 4중 보장의 DB 계층 유지).
#   - admin_data 는 map_admin 소유 → admin 레포 Alembic 이 이 DSN 으로
#     테이블(audit_logs/admin_accounts/admin_sessions)을 생성·관리한다.
# =========================================================================
set -euo pipefail

: "${MAP_ADMIN_PASSWORD:?MAP_ADMIN_PASSWORD must be set (infra .env)}"

# 따옴표 heredoc(<<'EOSQL')으로 $$·:'var' 를 psql 로 그대로 전달한다.
# 비밀번호는 psql 변수 :'mapadminpw' 로 안전하게 인용된다(-v 로 주입).
psql -v ON_ERROR_STOP=1 \
     -v mapadminpw="$MAP_ADMIN_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOSQL'
-- 역할이 없을 때만 생성(gexec: 생성 SQL 을 조건부 실행).
SELECT 'CREATE ROLE map_admin LOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'map_admin')
\gexec

-- 비밀번호는 매 실행마다 동기화(신규/기존 모두 idempotent).
ALTER ROLE map_admin LOGIN PASSWORD :'mapadminpw';

-- hub_data 읽기전용: 스키마 USAGE + 기존 테이블 SELECT + 미래 테이블 SELECT.
-- (최초 부팅 시엔 hub_data 테이블이 아직 없어 ALL TABLES 는 0건 부여되지만,
--  ALTER DEFAULT PRIVILEGES 가 이후 hub Alembic 이 만드는 테이블을 자동 커버.)
GRANT USAGE ON SCHEMA hub_data TO map_admin;
GRANT SELECT ON ALL TABLES IN SCHEMA hub_data TO map_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA hub_data GRANT SELECT ON TABLES TO map_admin;

-- admin_data 소유권 이전: map_admin 이 admin_data 를 소유(RW).
-- 00-create-schemas.sql 이 admin_data 를 superuser 소유로 생성해 두었으므로,
-- 여기서 소유권만 map_admin 으로 넘긴다. 소유자는 자동으로 전권(CREATE/DML)을
-- 가지므로 별도 GRANT 불필요. admin 레포 Alembic 이 이 스키마에 테이블을 만든다.
ALTER SCHEMA admin_data OWNER TO map_admin;

-- 편의용 search_path. admin_data 를 앞에 두어 Alembic 의 alembic_version(무수식
-- 생성)이 admin_data 에 안착하게 한다. hub_data 읽기는 뒤이어 계속 동작.
ALTER ROLE map_admin SET search_path = admin_data, hub_data, public;
EOSQL

echo "10-admin.sh: map_admin read-only role ensured on hub_data"
