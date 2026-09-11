\set ON_ERROR_STOP on
-- First installation only, after User finalization and Hub migrations.
-- GCP control accounts/audit remain on GCP. This creates no admin_data schema.
-- No passwords and no LOGIN activation: private transport/credential acceptance
-- must complete before a separate, identity-pinned operator enables either role.
BEGIN;
DO $preconditions$
BEGIN
  IF current_database() <> 'map_prod'
     OR to_regnamespace('user_service') IS NULL
     OR to_regnamespace('hub_data') IS NULL
     OR to_regclass('hub_data.alembic_version') IS NULL
     OR to_regnamespace('admin_data') IS NOT NULL THEN
    RAISE EXCEPTION 'NCP production schema boundary not ready';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ('map_prod_admin_readonly','map_prod_pg_exporter')) THEN
    RAISE EXCEPTION 'Existing production management role requires review';
  END IF;
  IF (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='hub_data') <> 'map_hub_owner' THEN
    RAISE EXCEPTION 'Hub schema ownership mismatch';
  END IF;
END
$preconditions$;

CREATE ROLE map_prod_admin_readonly NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 3;
CREATE ROLE map_prod_pg_exporter NOLOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 2;
GRANT CONNECT ON DATABASE map_prod TO map_prod_admin_readonly, map_prod_pg_exporter;
GRANT USAGE ON SCHEMA hub_data, public TO map_prod_admin_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA hub_data TO map_prod_admin_readonly;
-- New Hub tables require another reviewed grant; no default grant to future data.
GRANT pg_monitor TO map_prod_pg_exporter;
ALTER ROLE map_prod_admin_readonly SET default_transaction_read_only = on;
ALTER ROLE map_prod_admin_readonly SET statement_timeout = '10s';
ALTER ROLE map_prod_admin_readonly SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE map_prod_pg_exporter SET statement_timeout = '10s';

DO $privilege_boundary$
DECLARE observer text;
BEGIN
  FOREACH observer IN ARRAY ARRAY['map_prod_admin_readonly','map_prod_pg_exporter'] LOOP
    IF has_database_privilege(observer,current_database(),'CREATE')
       OR has_database_privilege(observer,current_database(),'TEMP')
       OR EXISTS (SELECT 1 FROM pg_namespace WHERE has_schema_privilege(observer,oid,'CREATE'))
       OR EXISTS (
         SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
         WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg\_%'
           AND c.relkind IN ('r','p','v','m','f')
           AND (has_table_privilege(observer,c.oid,'INSERT') OR has_table_privilege(observer,c.oid,'UPDATE')
             OR has_table_privilege(observer,c.oid,'DELETE') OR has_table_privilege(observer,c.oid,'TRUNCATE')
             OR (n.nspname <> 'hub_data' OR observer = 'map_prod_pg_exporter')
                AND (has_table_privilege(observer,c.oid,'SELECT') OR has_any_column_privilege(observer,c.oid,'SELECT'))
                -- PostGIS reference tables may be readable by PUBLIC. Only
                -- catalog-proven PostGIS extension members get this exception;
                -- an unrelated public table with the same name is not trusted.
                AND NOT (n.nspname='public' AND EXISTS (
                  SELECT 1 FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid
                  WHERE d.classid='pg_class'::regclass AND d.objid=c.oid
                    AND d.refclassid='pg_extension'::regclass AND d.deptype='e'
                    AND e.extname='postgis')))
       ) THEN
      RAISE EXCEPTION 'Production management privilege boundary violation';
    END IF;
  END LOOP;
END
$privilege_boundary$;
COMMIT;
