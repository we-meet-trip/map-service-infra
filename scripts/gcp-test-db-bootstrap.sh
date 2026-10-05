#!/usr/bin/env bash
# Empty-host PostgreSQL bootstrap for the GCP test VM "map-test" (database map_test).
#
# Brings a brand-new test database to the state in which the normal per-service
# migration jobs (cloud-up.sh -> service-migration-job.py for user, hub, agent)
# succeed and the serving containers pass their own privilege guards:
#   P1  cluster first boot with no init scripts and no application database
#   P2  map_test from template0 with an independent bootstrap marker
#   P3  User prepare SQL            (one-use bootstrap login, NOLOGIN final roles)
#   P4  UserBootstrapApplication    (exact bundle User image, isolated network)
#   P5  User finalize SQL           (bootstrap login retired, ownership moved)
#   P6  User activate SQL           (runtime/migrator LOGIN with Secret Manager values)
#   P7  normal Compose config for postgres, migration env files, User migrate+validate
#   P8  PostGIS, db/init content, Hub/Agent role SQL, map_admin, map_pg_exporter, passwords
#   P9  first Hub migration under a temporary database CREATE grant, Agent migration,
#       role SQL re-applied after each migration, final probes
#
# Run as root on the VM from its reviewed checkout, for example
#   gcloud compute ssh map-test --project mapservice-test --zone us-central1-a \
#     --tunnel-through-iap --command 'sudo bash \
#     /srv/map-test/map-service-infra/scripts/gcp-test-db-bootstrap.sh run \
#     --bundle DIR --run-id ID --ci-artifact ZIP --ci-artifact-digest sha256:HEX \
#     --sql-pins FILE --approve-postgis-view-revoke'
# The script also runs from stdin ("sudo bash -s -- run ..."); no command reads stdin.
# Procedure, inputs and recovery: docs/GCP_TEST_DB_BOOTSTRAP.md.
#
# Subcommands
#   check        validate every input; changes no container and no database
#   run          bootstrap; each phase reads the current state and skips completed work
#   verify       read-only postconditions and password-authentication probes
#   quarantine   retire the one-use bootstrap login after an interrupted attempt
#   postgis-acl  withdraw PUBLIC SELECT on the two PostGIS views again (for example
#                after ALTER EXTENSION postgis UPDATE); usable while the stack runs
#
# Inputs are pinned: the bundle by release_manifest.py including its registry check
# (each image's OCI revision and version labels), the User CI evidence by the GitHub
# artifact digest of its zip, the six fetched SQL files by SHA-256 values that a
# workstation computed at the exact commits (root 0600 file, one
# "<sha256>  we-meet-trip/map-service-<repo>@<commit>:<path>" line per file). Every
# input path, and every directory above it, must be root-owned and not writable by
# group or others, so nothing can replace an input after it was checked.
#
# The one-use User bootstrap (P3-P5) is never retried automatically. A failure
# there stops the exact job, runs the User quarantine SQL, terminates only the
# inventoried bootstrap sessions and leaves the database for manual review.
#
# Secret values never reach argv, output or logs. Secret Manager values are
# materialized into a 0600 file on tmpfs, read into shell variables and the file
# is deleted at once. psql receives them as \set lines on stdin, the bootstrap
# job reads them from its stdin, and migration jobs read root 0600 env files.
# Operator and probe sessions resolve functions only from pg_catalog, so an object
# a service owner created in its own schema is never called by them.
set -euo pipefail
umask 077
case $- in *x*) printf 'refusing to run with xtrace enabled\n' >&2; exit 2 ;; esac

PROJECT_ID=mapservice-test
REPO=/srv/map-test/map-service-infra
ENV_FILE=$REPO/.env.test
STATE=/var/lib/map-db-bootstrap
SQL_DIR=$STATE/sql
DEPLOY_STATE=/var/lib/map-deploy
ETC_DIR=/etc/map-deploy
LIB_DIR=/usr/local/lib/map-deploy
DB=map_test
COMPOSE_PROJECT=map-test
PG_VOLUME=map-test_postgres-data
BOOT_NET=map-test-user-bootstrap
JOB_KIND=user-bootstrap-v1
GH_RAW=https://raw.githubusercontent.com/we-meet-trip
SAFE_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
BOOT_RESULT='{"status":"bootstrap_complete","migrations_executed":4,"operator_finalization_required":true}'
SAFE_VALUE_RE='^[A-Za-z0-9._~+/=:@?&%,-]+$'
JOB_OUTPUT_RE='^[{]"status":"[a-z_]+"(,"[a-z_]+":("[a-z_]+"|[0-9]+|true|false))*[}]$'
# The watchdog takes the deployment lock for a moment on every cycle; a receive or a
# backup holds it far longer than this wait.
DEPLOY_LOCK_SECONDS=15

LOG=/dev/null
CURRENT_PHASE=P0_preflight
CMD=
BUNDLE=
RUN_ID=
CI_ARTIFACT=
CI_ARTIFACT_DIGEST=
SQL_PINS=
APPROVE_REVOKE=0
PG_ID=
PG_OPERATOR=
MARKER=
BOOT_PW=
ATTEMPT_ID=
JOB_ID=
PREPARED=0
IN_USER_ATTEMPT=0
HUB_WINDOW_OPEN=0
SECRETS_DIR=
RELEASE_TAG='' USER_SHA='' USER_REF='' HUB_SHA='' HUB_REF='' AGENT_SHA='' AGENT_REF='' RELEASE_SHA256=''
USER_IMAGE_ID='' HUB_IMAGE_ID='' AGENT_IMAGE_ID='' CI_DIGEST='' SQL_PINS_SHA256=''
PW_USER_RUNTIME='' PW_USER_MIGRATOR='' PW_HUB_RUNTIME='' PW_HUB_MIGRATOR=''
PW_AGENT_RUNTIME='' PW_AGENT_MIGRATOR='' PW_ADMIN='' PW_EXPORTER=''

# ---------------------------------------------------------------- output

say() {
  printf '%s\n' "$*" >>"$LOG" 2>/dev/null || true
  printf '%s\n' "$*" >&2 2>/dev/null || true
}

note() { say "{\"phase\":\"$CURRENT_PHASE\",\"result\":\"$1\"${2:+,$2}}"; }

die() {
  say "{\"phase\":\"$CURRENT_PHASE\",\"result\":\"HOLD\",\"code\":\"$1\",\"automatic_retry\":false}"
  exit 1
}

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

random_hex() { python3 -I -c 'import secrets, sys; print(secrets.token_hex(int(sys.argv[1])))' "$1"; }

# Write-once JSON record; values are fixed words, hex digests and timestamps only.
write_once() { ( set -o noclobber; printf '%s\n' "$2" >"$1" ) 2>>"$LOG" || die "record_exists_$(basename "$1" .json)"; }

usage_exit() {
  printf 'usage: gcp-test-db-bootstrap.sh {check|run|verify|quarantine|postgis-acl} [--bundle DIR --run-id ID --ci-artifact ZIP --ci-artifact-digest sha256:HEX --sql-pins FILE] [--approve-postgis-view-revoke]\n' >&2
  exit 2
}

# ---------------------------------------------------------------- python helper

pyh() {
  cat <<'PY'
import hashlib, json, os, re, stat, sys, urllib.parse, zipfile

def out(obj):
    print(json.dumps(obj, sort_keys=True))

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()

def env_values(path):
    values = {}
    with open(path, encoding="utf-8") as stream:
        for line in stream.read().splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value
    return values

def secrets_cmd(source, env_file, out_dir, database):
    sm, env = env_values(source), env_values(env_file)
    problems = []
    safe = re.compile(r"[A-Za-z0-9._~+/=:@?&%,-]+")
    hex64 = re.compile(r"[a-f0-9]{64}")
    ident = re.compile(r"[a-z_][a-z0-9_]{0,62}")

    def need(condition, code):
        if not condition:
            problems.append(code)

    def dsn_password(key, schemes, login, query_allowed=False):
        raw = sm.get(key, "")
        try:
            parts = urllib.parse.urlsplit(raw)
            port = parts.port or 5432
        except ValueError:
            problems.append(key + ":unparsable")
            return ""
        ok = (parts.scheme in schemes and parts.hostname == "postgres" and port == 5432
              and parts.path == "/" + database and parts.username is not None
              and urllib.parse.unquote(parts.username) == login and bool(parts.password)
              and not parts.fragment and (query_allowed or not parts.query))
        if not ok:
            problems.append(key + ":login_or_target_mismatch")
            return ""
        return urllib.parse.unquote(parts.password)

    hub_runtime = ("postgresql+psycopg", "postgresql+psycopg_async", "postgresql+asyncpg", "postgresql")
    passwords = {
        "PW_USER_RUNTIME": sm.get("USER_DATABASE_PASSWORD", ""),
        "PW_USER_MIGRATOR": sm.get("USER_MIGRATION_PASSWORD", ""),
        "PW_HUB_RUNTIME": dsn_password("HUB_DATABASE_URL", hub_runtime, "map_hub_runtime"),
        "PW_HUB_MIGRATOR": dsn_password("HUB_MIGRATION_DATABASE_URL", ("postgresql+psycopg", "postgresql"), "map_hub_migrator"),
        "PW_AGENT_RUNTIME": sm.get("AGENT_DATABASE_PASSWORD", ""),
        "PW_AGENT_MIGRATOR": dsn_password("AGENT_CHECKPOINT_MIGRATION_DSN", ("postgresql", "postgres"), "map_agent_migrator"),
        "PW_ADMIN": sm.get("MAP_ADMIN_PASSWORD", ""),
        "PW_EXPORTER": dsn_password("POSTGRES_EXPORTER_DSN", ("postgresql", "postgres"), "map_pg_exporter", True),
    }
    admin_url = dsn_password("ADMIN_DATABASE_URL", hub_runtime, "map_admin")
    need(admin_url == passwords["PW_ADMIN"], "ADMIN_DATABASE_URL:password_differs_from_MAP_ADMIN_PASSWORD")
    for key, value in passwords.items():
        need(bool(value) and safe.fullmatch(value) is not None, key + ":empty_or_unsafe_characters")
    need(hex64.fullmatch(passwords["PW_USER_RUNTIME"]) is not None, "USER_DATABASE_PASSWORD:not_64_lowercase_hex")
    need(hex64.fullmatch(passwords["PW_USER_MIGRATOR"]) is not None, "USER_MIGRATION_PASSWORD:not_64_lowercase_hex")
    need(len(set(passwords.values())) == len(passwords), "role_passwords_not_distinct")
    need(bool(sm.get("POSTGRES_PASSWORD")) and sm.get("POSTGRES_PASSWORD") not in passwords.values(),
         "POSTGRES_PASSWORD:empty_or_reused_by_a_service_role")
    for key in ("POSTGRES_PASSWORD", "USER_DATABASE_PASSWORD", "AGENT_DATABASE_PASSWORD", "HUB_DATABASE_URL",
                "MAP_ADMIN_PASSWORD", "ADMIN_DATABASE_URL", "POSTGRES_EXPORTER_DSN"):
        need(env.get(key) == sm.get(key), key + ":env_test_differs_from_secret_manager")
    for key in ("USER_MIGRATION_URL", "USER_MIGRATION_USERNAME", "USER_MIGRATION_PASSWORD",
                "HUB_MIGRATION_DATABASE_URL", "AGENT_CHECKPOINT_MIGRATION_DSN"):
        need(key not in env, key + ":must_not_be_in_env_test")
    operator = env.get("POSTGRES_USER", "")
    need(env.get("POSTGRES_DB") == database, "POSTGRES_DB:must_be_" + database)
    need(ident.fullmatch(operator) is not None and not operator.startswith("map_"), "POSTGRES_USER:invalid_operator")
    need(env.get("POSTGRES_HOST", "postgres") == "postgres" and env.get("POSTGRES_PORT", "5432") == "5432",
         "POSTGRES_HOST_PORT:must_be_postgres_5432")
    need(env.get("USER_DATABASE_USER") == "map_user_runtime", "USER_DATABASE_USER:must_be_map_user_runtime")
    need(env.get("AGENT_DATABASE_USER") == "map_agent_runtime", "AGENT_DATABASE_USER:must_be_map_agent_runtime")
    need(env.get("LANGGRAPH_SCHEMA", "langgraph") == "langgraph", "LANGGRAPH_SCHEMA:must_be_langgraph")
    if problems:
        out({"secrets": "rejected", "problems": sorted(set(problems))})
        return 1

    def write(name, text):
        descriptor = os.open(os.path.join(out_dir, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)

    write("roles.env", "".join(key + "=" + value + "\n" for key, value in passwords.items()))
    write("user-migration.env", "USER_MIGRATION_URL=jdbc:postgresql://postgres:5432/" + database
          + "?currentSchema=user_service\nUSER_MIGRATION_USERNAME=map_user_migrator\n"
          + "USER_MIGRATION_PASSWORD=" + passwords["PW_USER_MIGRATOR"] + "\n")
    write("hub-migration.env", "HUB_MIGRATION_DATABASE_URL=" + sm["HUB_MIGRATION_DATABASE_URL"] + "\n")
    write("agent-migration.env", "AGENT_CHECKPOINT_MIGRATION_DSN=" + sm["AGENT_CHECKPOINT_MIGRATION_DSN"] + "\n")
    out({"secrets": "validated", "role_passwords": sorted(passwords), "migration_env_files": 3})
    return 0

def bundle_cmd(bundle):
    path = os.path.join(bundle, "release.json")
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    fields = [data["release_tag"]]
    for name in ("user", "hub", "agent"):
        entry = data["services"][name]
        ref = entry["image"] + "@" + entry["digest"]
        if (not re.fullmatch(r"ghcr\.io/we-meet-trip/map-service-%s@sha256:[a-f0-9]{64}" % name, ref)
                or not re.fullmatch(r"[a-f0-9]{40}", entry["source_sha"])):
            return 1
        fields += [entry["source_sha"], ref]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", data["release_tag"]):
        return 1
    fields.append(sha256_file(path))
    print(" ".join(fields))
    return 0

REQUIRED_CI_CHECKS = ("genuine_v001_v004", "bootstrap_login_password_revoked",
                      "genuine_runtime_migrator_activation", "normal_validate", "unchanged_runtime_guard_pass")
CI_MEMBER = "user-bootstrap-postgres-verification.json"

# The zip exactly as GitHub serves it: its SHA-256 is the artifact digest GitHub lists.
def artifact_cmd(path, digest, source_sha):
    if os.path.getsize(path) > 1 << 20 or "sha256:" + sha256_file(path) != digest:
        out({"ci_artifact": "rejected", "reason": "digest_mismatch"})
        return 1
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if (len(members) != 1 or members[0].filename != CI_MEMBER or members[0].file_size > 1 << 20
                or stat.S_ISLNK(members[0].external_attr >> 16)):
            out({"ci_artifact": "rejected", "reason": "unexpected_members"})
            return 1
        data = json.loads(archive.read(members[0]).decode("utf-8"))
    passed = {item.get("name") for item in data.get("checks", []) if isinstance(item, dict) and item.get("pass") is True}
    ok = (data.get("success") is True and data.get("source") == source_sha and "failed_phase" not in data
          and all(name in passed for name in REQUIRED_CI_CHECKS))
    if not ok:
        out({"ci_artifact": "rejected", "required_checks": list(REQUIRED_CI_CHECKS)})
        return 1
    print(digest)
    return 0

def pgconfig_cmd(mode, operator, init_source):
    config = json.load(sys.stdin)
    service = config["services"]["postgres"]
    environment = service.get("environment") or {}
    database = "postgres" if mode == "override" else "map_test"
    volumes = sorted((item.get("type"), item.get("source"), item.get("target"), bool(item.get("read_only")))
                     for item in service.get("volumes", []))
    expected = sorted([("volume", "postgres-data", "/var/lib/postgresql/data", False),
                       ("bind", init_source, "/docker-entrypoint-initdb.d", True)])
    checks = {
        "project": config.get("name") == "map-test",
        "image": service.get("image") == "postgis/postgis:17-3.5",
        "database": environment.get("POSTGRES_DB") == database,
        "operator": environment.get("POSTGRES_USER") == operator,
        "volumes": volumes == expected,
        "volume_name": ((config.get("volumes") or {}).get("postgres-data") or {}).get("name") == "map-test_postgres-data",
        "healthcheck": (service.get("healthcheck") or {}).get("test")
                       == ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U %s -d %s" % (operator, database)],
    }
    unexpected = sorted(key for key, ok in checks.items() if not ok)
    out({"postgres_config": mode, "unexpected": unexpected})
    return 1 if unexpected else 0

def receipt_cmd(path, service, operation, image):
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    ok = (data.get("status") == "PASS" and data.get("project") == "map-test" and data.get("service") == service
          and data.get("operation") == operation and data.get("image") == image
          and result.get("status") == "complete" and data.get("database_container_preserved") is True
          and data.get("temporary_job_removed") is True and data.get("network_database_only_after_job") is True)
    out({"receipt": os.path.basename(path), "pass": ok, "migrations_executed": result.get("migrations_executed")})
    return 0 if ok else 1

# The lstat of an absolute path whose every component, from / down, is root-owned, not a
# link and not writable by group or others; None otherwise. Only root can then replace
# what the path names. "." and ".." components are refused, so the components checked
# are the ones the kernel resolves.
def root_only(path):
    path = path.rstrip("/") or "/"
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        return None
    parts = path.split("/")[1:] if path != "/" else []
    for depth in range(len(parts) + 1):
        info = os.lstat("/" + "/".join(parts[:depth]))
        if info.st_uid != 0 or stat.S_ISLNK(info.st_mode) or info.st_mode & 0o022:
            return None
    return info

def owned_cmd(path):
    return 0 if root_only(path) is not None else 1

def private_file_cmd(path):
    info = root_only(path)
    return 0 if (info is not None and stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                 and info.st_nlink == 1) else 1

def field_cmd(path, key):
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream).get(key)
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:@/-]{1,200}", value):
        return 1
    print(value)
    return 0

COMMANDS = {"secrets": secrets_cmd, "bundle": bundle_cmd, "artifact": artifact_cmd, "pgconfig": pgconfig_cmd,
            "receipt": receipt_cmd, "owned": owned_cmd, "private-file": private_file_cmd, "field": field_cmd}
try:
    sys.exit(COMMANDS[sys.argv[1]](*sys.argv[2:]))
except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, zipfile.BadZipFile):
    sys.exit(1)
PY
}

py() { python3 -I -c "$(pyh)" "$@"; }

# ---------------------------------------------------------------- docker and psql

compose() {
  env -i PATH="$SAFE_PATH" HOME=/root docker compose --project-directory "$REPO" --env-file "$ENV_FILE" \
    -f "$REPO/docker-compose.yml" -f "$REPO/docker-compose.test.yml" "$@"
}

locate_pg() {
  local ids
  ids=$(docker ps -q --no-trunc --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
    --filter label=com.docker.compose.service=postgres)
  case $ids in '' | *[!a-f0-9]*) die one_running_postgres_required ;; esac
  [ ${#ids} -eq 64 ] || die one_running_postgres_required
  PG_ID=$ids
}

# Operator session over the container's local socket (trusted by initdb).
op_sql() {
  docker exec -i "$PG_ID" psql -X -q -A -t -v ON_ERROR_STOP=1 -v VERBOSITY=terse -v SHOW_CONTEXT=never \
    -U "$PG_OPERATOR" -d "$1" 2>>"$LOG"
}

# Password session over TCP to the private "postgres" name: proves SCRAM login with the given value.
tcp_sql() {
  { printf '%s\n' "$2"; cat; } | docker exec -i "$PG_ID" sh -c 'IFS= read -r PGPASSWORD; export PGPASSWORD PGCONNECT_TIMEOUT=10; exec psql -X -q -A -t -v ON_ERROR_STOP=1 -v VERBOSITY=terse -v SHOW_CONTEXT=never -h postgres -U "$1" -d "$2"' sh "$1" "$DB" 2>>"$LOG"
}

psql_var() {
  [[ $2 =~ $SAFE_VALUE_RE ]] || die psql_value_unsafe
  printf '\\set %s '"'"'%s'"'"'\n' "$1" "$2"
}

# Keeps statement text (which may hold a literal secret) out of the server log, and
# resolves unqualified functions only in pg_catalog: the operator role's default
# search_path names schemas whose owners are service roles.
sql_quiet() {
  cat <<'SQL'
SET search_path = pg_catalog, pg_temp;
SET application_name = 'map-db-bootstrap';
SET password_encryption = 'scram-sha-256';
SET log_error_verbosity = terse;
SET log_min_error_statement = panic;
SET log_statement = none;
SET log_min_duration_statement = -1;
SQL
}

expect_rows() {
  local code=$1 expected=$2 database=$3 got
  shift 3
  got=$({ sql_quiet; "$@"; } | op_sql "$database") || die "${code}_query_failed"
  [ "$got" = "$expected" ] || die "$code"
}

expect_tcp() {
  local code=$1 role=$2 password=$3 got
  shift 3
  got=$({ printf '%s\n' 'SET search_path = pg_catalog, pg_temp;'; "$@"; } | tcp_sql "$role" "$password") \
    || die "${code}_query_failed"
  [ "$got" = t ] || die "$code"
}

apply_sql() {
  local code=$1
  shift
  { sql_quiet; "$@"; } | op_sql "$DB" >/dev/null || die "$code"
}

ensure_image() {
  local id
  if ! id=$(docker image inspect --format '{{.Id}}' "$1" 2>/dev/null); then
    timeout 900 docker pull --quiet "$1" >>"$LOG" 2>&1 || die image_pull_failed
    id=$(docker image inspect --format '{{.Id}}' "$1" 2>>"$LOG") || die image_missing_after_pull
  fi
  case $id in sha256:*) printf '%s\n' "$id" ;; *) die image_id_invalid ;; esac
}

# ---------------------------------------------------------------- SQL text

# Fetched SQL is cached per repository commit, so a check of one bundle never blocks a
# run of another.
sql_file() {
  case $1 in
    user-*) cat "$SQL_DIR/user-$USER_SHA/$1" ;;
    hub-*) cat "$SQL_DIR/hub-$HUB_SHA/$1" ;;
    agent-*) cat "$SQL_DIR/agent-$AGENT_SHA/$1" ;;
    *) return 1 ;;
  esac
}

sql_fresh_cluster() {
  psql_var op "$PG_OPERATOR"
  cat <<'SQL'
SELECT current_user = :'op'
   AND (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
   AND current_setting('server_version_num')::int >= 170000
   AND (SELECT array_agg(datname::text ORDER BY datname) FROM pg_database) = ARRAY['postgres','template0','template1']
   AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname !~ '^pg_' AND rolname <> current_user);
SQL
}

sql_db_exists() {
  psql_var db "$DB"
  printf '%s\n' "SELECT count(*) FROM pg_database WHERE datname = :'db';"
}

# Every backend on the target, background workers included (prepare refuses them all).
sql_other_sessions() {
  psql_var db "$DB"
  printf '%s\n' "SELECT count(*) FROM pg_stat_activity WHERE datname = :'db' AND pid <> pg_backend_pid();"
}

sql_create_db() {
  psql_var db "$DB"
  printf '%s\n' "SELECT format('CREATE DATABASE %I TEMPLATE template0', :'db') \\gexec"
}

sql_comment_db() {
  psql_var db "$DB"
  psql_var marker "$MARKER"
  printf '%s\n' "SELECT format('COMMENT ON DATABASE %I IS %L', :'db', 'map-user-bootstrap:v1:' || :'marker') \\gexec"
}

sql_pristine() {
  cat <<'SQL'
SELECT NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT IN ('public','information_schema') AND nspname NOT LIKE 'pg\_%' ESCAPE '\')
   AND NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql')
   AND NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\_%' ESCAPE '\');
SQL
}

sql_user_state() {
  psql_var marker "$MARKER"
  cat <<'SQL'
SELECT CASE
  WHEN shobj_description(d.oid, 'pg_database') IS NULL THEN 'unmarked'
  WHEN shobj_description(d.oid, 'pg_database') <> 'map-user-bootstrap:v1:' || :'marker' THEN 'foreign'
  ELSE COALESCE((SELECT CASE obj_description(n.oid, 'pg_namespace')
                   WHEN 'map-user-bootstrap:v1:' || :'marker' || ':ready' THEN 'ready'
                   WHEN 'map-user-bootstrap:v1:' || :'marker' || ':started' THEN 'started'
                   WHEN 'map-user-bootstrap:v1:finalized' THEN
                     CASE WHEN (SELECT count(*) FROM pg_roles
                                 WHERE rolname IN ('map_user_runtime','map_user_migrator') AND rolcanlogin) = 2
                          THEN 'active' ELSE 'finalized' END
                   ELSE 'foreign' END
                 FROM pg_namespace n WHERE n.nspname = 'user_service'), 'marked')
END
FROM pg_database d WHERE d.datname = current_database();
SQL
}

sql_prepare() {
  psql_var expected_database "$DB"
  psql_var marker "$MARKER"
  psql_var bootstrap_password "$BOOT_PW"
  sql_file user-database-bootstrap-prepare.sql
}

sql_finalize() {
  psql_var expected_database "$DB"
  psql_var marker "$MARKER"
  sql_file user-database-bootstrap-finalize.sql
}

sql_activate() {
  psql_var expected_database "$DB"
  psql_var marker "$MARKER"
  psql_var runtime_password "$PW_USER_RUNTIME"
  psql_var migrator_password "$PW_USER_MIGRATOR"
  sql_file user-database-bootstrap-activate.sql
}

sql_quarantine() {
  psql_var expected_database "$DB"
  psql_var marker "$MARKER"
  sql_file user-database-bootstrap-quarantine.sql
}

sql_bootstrap_role_exists() { printf '%s\n' "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'map_user_bootstrap');"; }

sql_bootstrap_sessions() { printf '%s\n' "SELECT count(*) FROM pg_stat_activity WHERE usename = 'map_user_bootstrap';"; }

sql_bootstrap_session_inventory() {
  cat <<'SQL'
SELECT pid || ' ' || (extract(epoch FROM backend_start) * 1000000)::bigint FROM pg_stat_activity
 WHERE datname = current_database() AND usename = 'map_user_bootstrap'
   AND application_name IN ('map-user-bootstrap','map-user-privilege-check');
SQL
}

sql_terminate_session() {
  psql_var pid "$1"
  psql_var started "$2"
  cat <<'SQL'
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE datname = current_database() AND usename = 'map_user_bootstrap'
   AND application_name IN ('map-user-bootstrap','map-user-privilege-check')
   AND pid = :'pid'::int AND (extract(epoch FROM backend_start) * 1000000)::bigint = :'started'::bigint;
SQL
}

sql_bootstrap_retired() {
  cat <<'SQL'
SELECT NOT rolcanlogin AND rolpassword IS NULL
   AND NOT has_database_privilege('map_user_bootstrap', current_database(), 'CONNECT')
   AND NOT EXISTS (SELECT 1 FROM pg_stat_activity WHERE usename = 'map_user_bootstrap')
  FROM pg_authid WHERE rolname = 'map_user_bootstrap';
SQL
}

sql_scram_guard() {
  cat <<'SQL'
SELECT NOT EXISTS (SELECT 1 FROM pg_hba_file_rules WHERE error IS NOT NULL OR
  (type LIKE 'host%' AND auth_method <> 'scram-sha-256' AND NOT
   ((address = '127.0.0.1' AND netmask = '255.255.255.255') OR
    (address = '::1' AND netmask = 'ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'))));
SQL
}

sql_user_legacy_history() {
  cat <<'SQL'
WITH h AS (SELECT * FROM user_service.flyway_schema_history ORDER BY installed_rank LIMIT 4)
SELECT count(*) = 4
   AND bool_and(success AND installed_by = 'map_user_bootstrap')
   AND array_agg(version::text ORDER BY installed_rank) = ARRAY['001','002','003','004']
   AND array_agg(checksum ORDER BY installed_rank) = ARRAY[-192854188,-2113231432,1973132559,-777713445]
  FROM h;
SQL
}

sql_user_history_rows() { printf '%s\n' "SELECT count(*) FROM user_service.flyway_schema_history;"; }

NET_SQL="inet_server_addr() IS NOT NULL AND NOT (inet_server_addr() << inet '127.0.0.0/8') AND inet_server_addr() <> inet '::1'"

sql_probe_user_runtime() {
  cat <<SQL
SELECT current_user = 'map_user_runtime' AND session_user = current_user AND $NET_SQL
   AND has_database_privilege(current_database(), 'CONNECT')
   AND NOT has_database_privilege(current_database(), 'CREATE,TEMP')
   AND has_table_privilege('user_service.users', 'SELECT') AND has_table_privilege('user_service.users', 'INSERT')
   AND has_table_privilege('user_service.users', 'UPDATE') AND has_table_privilege('user_service.users', 'DELETE')
   AND NOT has_table_privilege('user_service.flyway_schema_history', 'INSERT,UPDATE,DELETE');
SQL
}

sql_probe_owner_via_migrator() {
  cat <<SQL
SET ROLE $1;
SELECT current_user = '$1' AND session_user = '$2' AND $NET_SQL
   AND NOT has_database_privilege(current_database(), 'CREATE,TEMP');
SQL
}

sql_probe_service_runtime() {
  local role=$1 owner=$2 schema=$3 history=$4 data=$5 action checks=
  for action in SELECT INSERT UPDATE DELETE; do
    checks="$checks AND has_table_privilege('$schema.$data', '$action')"
  done
  cat <<SQL
SELECT current_user = '$role' AND session_user = current_user AND $NET_SQL
   AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls AND NOT rolinherit
   AND NOT has_database_privilege(current_database(), 'CREATE,TEMP')
   AND NOT has_schema_privilege('$schema', 'CREATE') AND NOT has_schema_privilege('public', 'CREATE')
   AND NOT pg_has_role(current_user, '$owner', 'MEMBER')
   AND has_table_privilege('$schema.$history', 'SELECT')
   AND NOT has_table_privilege('$schema.$history', 'INSERT,UPDATE,DELETE')
   $checks
   AND NOT has_table_privilege((SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                                 WHERE n.nspname = 'user_service' AND c.relname = 'users'), 'SELECT,INSERT,UPDATE,DELETE')
  FROM pg_roles WHERE rolname = current_user;
SQL
}

sql_probe_hub_postgis() {
  printf '%s\n' "SELECT public.ST_DWithin(public.ST_SetSRID(public.ST_MakePoint(127,37),4326)::public.geography, public.ST_SetSRID(public.ST_MakePoint(127,37),4326)::public.geography, 1);"
}

sql_probe_exporter() {
  cat <<SQL
SELECT current_user = 'map_pg_exporter' AND session_user = current_user AND $NET_SQL
   AND pg_has_role('pg_monitor', 'MEMBER')
   AND NOT has_database_privilege(current_database(), 'CREATE,TEMP')
   AND NOT has_schema_privilege('public', 'CREATE');
SQL
}

sql_probe_admin() {
  cat <<SQL
SELECT current_user = 'map_admin' AND session_user = current_user AND $NET_SQL
   AND has_schema_privilege('hub_data', 'USAGE') AND has_schema_privilege('public', 'USAGE')
   AND (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'admin_data') = 'map_admin'
   AND NOT has_database_privilege(current_database(), 'CREATE,TEMP')
   AND public.ST_AsText(public.ST_SetSRID(public.ST_MakePoint(127,37),4326)) = 'POINT(127 37)';
SQL
}

# The same cross-schema queries the User runtime and migrator guards run, evaluated
# with the operator switched to each role. Both lines must read t.
sql_user_guard_dry_run() {
  local reference data sequence definer role
  reference="(n.nspname='public' AND c.relname='spatial_ref_sys' AND EXISTS (SELECT 1 FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.refclassid='pg_extension'::regclass AND d.deptype='e' AND e.extname='postgis'))"
  data="NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname<>'user_service' AND n.nspname<>'information_schema' AND n.nspname NOT LIKE 'pg_%' AND c.relkind IN ('r','p','v','m','f') AND ((NOT $reference AND (has_table_privilege(c.oid,'SELECT') OR has_any_column_privilege(c.oid,'SELECT'))) OR has_table_privilege(c.oid,'INSERT') OR has_table_privilege(c.oid,'UPDATE') OR has_table_privilege(c.oid,'DELETE') OR has_table_privilege(c.oid,'TRUNCATE') OR has_table_privilege(c.oid,'REFERENCES') OR has_table_privilege(c.oid,'TRIGGER') OR has_table_privilege(c.oid,'MAINTAIN') OR has_any_column_privilege(c.oid,'INSERT') OR has_any_column_privilege(c.oid,'UPDATE') OR has_any_column_privilege(c.oid,'REFERENCES')))"
  sequence="NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname<>'user_service' AND n.nspname<>'information_schema' AND n.nspname NOT LIKE 'pg_%' AND c.relkind='S' AND (has_sequence_privilege(c.oid,'USAGE') OR has_sequence_privilege(c.oid,'SELECT') OR has_sequence_privilege(c.oid,'UPDATE') OR has_sequence_privilege(c.oid,'SELECT WITH GRANT OPTION') OR has_sequence_privilege(c.oid,'USAGE WITH GRANT OPTION')))"
  definer="NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname<>'information_schema' AND n.nspname NOT LIKE 'pg_%' AND p.prosecdef AND has_function_privilege(p.oid,'EXECUTE'))"
  for role in map_user_runtime map_user_owner; do
    printf 'SET ROLE %s;\nSELECT %s AND %s AND %s;\nRESET ROLE;\n' "$role" "$data" "$sequence" "$definer"
  done
}

# Catalog digest of the User contract (schema, objects, roles, verifiers, grants,
# defaults, history). Returns only an aggregate hash.
sql_user_fingerprint() {
  cat <<'SQL'
SELECT encode(sha256(convert_to(jsonb_build_object(
 'schema',(SELECT jsonb_build_array(nspowner,nspacl) FROM pg_namespace WHERE nspname='user_service'),
 'relations',(SELECT jsonb_agg(jsonb_build_array(c.relname,c.relkind,c.relowner,c.relacl) ORDER BY c.relname)
   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='user_service'),
 'routines',(SELECT jsonb_agg(jsonb_build_array(p.oid,p.proowner,p.proacl) ORDER BY p.oid)
   FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='user_service'),
 'roles',(SELECT jsonb_agg(jsonb_build_array(rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,
       rolreplication,rolbypassrls,rolinherit,rolvaliduntil,rolconfig,
       has_database_privilege(oid,current_database(),'CONNECT'),
       has_database_privilege(oid,current_database(),'CREATE'),has_database_privilege(oid,current_database(),'TEMP'),
       has_schema_privilege(oid,'public','USAGE'),has_schema_privilege(oid,'public','CREATE')) ORDER BY rolname)
   FROM pg_roles WHERE rolname IN ('map_user_bootstrap','map_user_owner','map_user_migrator','map_user_runtime')),
 'credential_verifiers',(SELECT jsonb_agg(jsonb_build_array(rolname,md5(rolpassword)) ORDER BY rolname)
   FROM pg_authid WHERE rolname IN ('map_user_bootstrap','map_user_owner','map_user_migrator','map_user_runtime')),
 'memberships',(SELECT jsonb_agg(jsonb_build_array(roleid,member,admin_option,inherit_option,set_option) ORDER BY roleid,member)
   FROM pg_auth_members WHERE member IN (SELECT oid FROM pg_roles WHERE rolname IN ('map_user_bootstrap','map_user_owner','map_user_migrator','map_user_runtime'))
      OR roleid IN (SELECT oid FROM pg_roles WHERE rolname IN ('map_user_bootstrap','map_user_owner','map_user_migrator','map_user_runtime'))),
 'defaults',(SELECT jsonb_agg(jsonb_build_array(defaclrole,defaclnamespace,defaclobjtype,defaclacl) ORDER BY oid)
   FROM pg_default_acl WHERE defaclrole IN (SELECT oid FROM pg_roles WHERE rolname IN ('map_user_bootstrap','map_user_owner','map_user_migrator','map_user_runtime'))),
 'history',(SELECT jsonb_agg(to_jsonb(h) ORDER BY installed_rank) FROM user_service.flyway_schema_history h)
)::text,'UTF8')),'hex');
SQL
}

# PostGIS goes into public explicitly. Its two catalog views are PUBLIC-readable by
# the extension script; the User guards accept only spatial_ref_sys, so the same
# transaction withdraws PUBLIC SELECT on the views.
sql_postgis() {
  cat <<'SQL'
BEGIN;
CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public;
REVOKE SELECT ON TABLE public.geometry_columns, public.geography_columns FROM PUBLIC;
COMMIT;
SELECT e.extnamespace = 'public'::regnamespace
   AND public.ST_IsValid(public.ST_GeomFromText('POINT(127 37)', 4326))
   AND public.ST_SRID(public.ST_GeomFromText('POINT(127 37)', 4326)) = 4326
  FROM pg_extension e WHERE e.extname = 'postgis';
SQL
}

# The SQL body of db/init/10-admin.sh. The file's own first body line
# "\getenv mapadminpw MAP_ADMIN_PASSWORD" reads the password from the psql process
# environment; exactly that line is dropped here and the value arrives as a \set line
# on stdin instead, like every other secret. The rest is applied unchanged.
admin_sql_body() {
  awk -v start="<<'EOSQL'" -v getenv='\\getenv mapadminpw MAP_ADMIN_PASSWORD' '
    f && $0 == "EOSQL" {exit}
    f && !seen++ && $0 == getenv {next}
    f {print}
    length($0) >= length(start) && substr($0, length($0) - length(start) + 1) == start {f = 1}' \
    "$REPO/db/init/10-admin.sh"
}

sql_admin() {
  local body
  body=$(admin_sql_body)
  case $body in *"CREATE ROLE map_admin LOGIN"*":'mapadminpw'"*) ;; *) die admin_sql_body_unexpected ;; esac
  # psql treats a backslash anywhere outside quotes as a meta-command; only whole
  # \gexec lines are expected.
  if printf '%s\n' "$body" | grep -vxF '\gexec' | grep -qF "\\"; then
    die admin_sql_body_meta_command
  fi
  psql_var mapadminpw "$PW_ADMIN"
  printf '%s\n' "$body"
}

sql_supplementary() {
  cat <<'SQL'
BEGIN;
DO $exporter$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'map_pg_exporter') THEN
    CREATE ROLE map_pg_exporter LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END
$exporter$;
GRANT pg_monitor TO map_pg_exporter;
SELECT format('GRANT CONNECT ON DATABASE %I TO map_admin, map_pg_exporter', current_database()) \gexec
GRANT USAGE ON SCHEMA public TO map_hub_owner, map_admin;
DO $exporter_check$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'map_pg_exporter'
             AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls OR NOT rolinherit))
     OR (SELECT array_agg(roleid::regrole::text) FROM pg_auth_members
          WHERE member = 'map_pg_exporter'::regrole) IS DISTINCT FROM ARRAY['pg_monitor'] THEN
    RAISE EXCEPTION 'exporter login must hold only pg_monitor';
  END IF;
END
$exporter_check$;
COMMIT;
SQL
}

sql_service_passwords() {
  psql_var hub_runtime "$PW_HUB_RUNTIME"
  psql_var hub_migrator "$PW_HUB_MIGRATOR"
  psql_var agent_runtime "$PW_AGENT_RUNTIME"
  psql_var agent_migrator "$PW_AGENT_MIGRATOR"
  psql_var exporter "$PW_EXPORTER"
  cat <<'SQL'
BEGIN;
ALTER ROLE map_hub_runtime LOGIN PASSWORD :'hub_runtime';
ALTER ROLE map_hub_migrator LOGIN PASSWORD :'hub_migrator';
ALTER ROLE map_agent_runtime LOGIN PASSWORD :'agent_runtime';
ALTER ROLE map_agent_migrator LOGIN PASSWORD :'agent_migrator';
ALTER ROLE map_pg_exporter LOGIN PASSWORD :'exporter';
COMMIT;
SQL
}

sql_postconditions() {
  cat <<'SQL'
SELECT (SELECT count(*) FROM pg_authid
         WHERE rolname IN ('map_user_runtime','map_user_migrator','map_hub_runtime','map_hub_migrator',
                           'map_agent_runtime','map_agent_migrator','map_admin','map_pg_exporter')
           AND rolcanlogin AND rolpassword LIKE 'SCRAM-SHA-256$%' AND NOT rolsuper AND NOT rolcreatedb
           AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls) = 8
   AND (SELECT count(*) FROM pg_roles
         WHERE rolname IN ('map_user_owner','map_hub_owner','map_agent_owner','map_user_bootstrap') AND NOT rolcanlogin) = 4
   AND NOT EXISTS (SELECT 1 FROM pg_roles r WHERE r.rolname ~ '^map_'
                    AND (has_database_privilege(r.oid, current_database(), 'CREATE')
                         OR has_database_privilege(r.oid, current_database(), 'TEMP')))
   AND NOT has_database_privilege('public', current_database(), 'CONNECT')
   AND (SELECT array_agg(nspname || ':' || pg_get_userbyid(nspowner) ORDER BY nspname) FROM pg_namespace
         WHERE nspname IN ('admin_data','hub_data','langgraph','user_service'))
       = ARRAY['admin_data:map_admin','hub_data:map_hub_owner','langgraph:map_agent_owner','user_service:map_user_owner']
   AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname !~ '^pg_'
                    AND nspname NOT IN ('public','information_schema','admin_data','hub_data','langgraph','user_service'))
   AND NOT has_schema_privilege('public', 'public', 'USAGE')
   AND (SELECT array_agg(rolname::text ORDER BY rolname) FROM pg_roles
         WHERE rolname ~ '^map_' AND has_schema_privilege(oid, 'public', 'USAGE'))
       = ARRAY['map_admin','map_hub_owner','map_hub_runtime']
   AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname ~ '^map_' AND has_schema_privilege(oid, 'public', 'CREATE'))
   AND has_table_privilege('public', 'public.spatial_ref_sys', 'SELECT')
   AND NOT has_table_privilege('public', 'public.geometry_columns', 'SELECT')
   AND NOT has_table_privilege('public', 'public.geography_columns', 'SELECT')
   AND (SELECT e.extnamespace = 'public'::regnamespace FROM pg_extension e WHERE e.extname = 'postgis')
   AND (SELECT array_agg(roleid::regrole::text) FROM pg_auth_members WHERE member = 'map_pg_exporter'::regrole) = ARRAY['pg_monitor']
   AND NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE member = 'map_admin'::regrole);
SQL
}

sql_hub_scope_on() {
  psql_var db "$DB"
  cat <<'SQL'
BEGIN;
DO $expiry$ BEGIN EXECUTE format('ALTER ROLE map_hub_migrator VALID UNTIL %L', clock_timestamp() + interval '20 minutes'); END $expiry$;
SELECT format('GRANT CREATE ON DATABASE %I TO map_hub_owner', :'db') \gexec
COMMIT;
SELECT has_database_privilege('map_hub_owner', :'db', 'CREATE');
SQL
}

sql_hub_scope_off() {
  psql_var db "$DB"
  cat <<'SQL'
BEGIN;
SELECT format('REVOKE CREATE ON DATABASE %I FROM map_hub_owner', :'db') \gexec
ALTER ROLE map_hub_migrator VALID UNTIL 'infinity';
COMMIT;
SELECT has_database_privilege('map_hub_owner', :'db', 'CREATE');
SQL
}

sql_hub_create_present() {
  cat <<'SQL'
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'map_hub_owner')
            THEN has_database_privilege('map_hub_owner', current_database(), 'CREATE') ELSE false END;
SQL
}

sql_hub_initialized() { printf '%s\n' "SELECT to_regclass('hub_data.alembic_version') IS NOT NULL;"; }

sql_hub_data_relations() {
  printf '%s\n' "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'hub_data';"
}

# ---------------------------------------------------------------- inputs

parse_args() {
  CMD=${1:-}
  [ $# -gt 0 ] && shift
  while [ $# -gt 0 ]; do
    case $1 in
      --bundle) [ $# -ge 2 ] || usage_exit; BUNDLE=$2; shift 2 ;;
      --run-id) [ $# -ge 2 ] || usage_exit; RUN_ID=$2; shift 2 ;;
      --ci-artifact) [ $# -ge 2 ] || usage_exit; CI_ARTIFACT=$2; shift 2 ;;
      --ci-artifact-digest) [ $# -ge 2 ] || usage_exit; CI_ARTIFACT_DIGEST=$2; shift 2 ;;
      --sql-pins) [ $# -ge 2 ] || usage_exit; SQL_PINS=$2; shift 2 ;;
      --approve-postgis-view-revoke) APPROVE_REVOKE=1; shift ;;
      *) usage_exit ;;
    esac
  done
  case $CMD in check | run | verify | quarantine | postgis-acl) ;; *) usage_exit ;; esac
}

env_value() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }

init_state() {
  [ "$(id -u)" = 0 ] || { printf 'root required\n' >&2; exit 2; }
  [ "$(uname -s)" = Linux ] || { printf 'linux required\n' >&2; exit 2; }
  install -d -o root -g root -m 0700 "$STATE" "$STATE/receipts" "$SQL_DIR"
  install -d -o root -g root -m 0555 "$STATE/empty-initdb"
  LOG=$STATE/run.log
  touch "$LOG" && chmod 0600 "$LOG"
  say "{\"run\":\"$CMD\",\"at\":\"$(utc_now)\"}"
}

check_host() {
  local server compose_version
  if ! { py owned "$REPO" && [ -d "$REPO/.git" ]; }; then die repo_not_root_owned_checkout; fi
  # No optional locks: a status refresh would take index.lock, which a concurrent receive's
  # checkout needs.
  [ -z "$(git --no-optional-locks -C "$REPO" status --porcelain --untracked-files=no 2>>"$LOG")" ] \
    || die repo_has_tracked_changes
  for f in docker-compose.yml docker-compose.test.yml docker-compose.registry.yml db/init/00-create-schemas.sql \
    db/init/10-admin.sh scripts/gcp_secrets.py scripts/service-migration-job.py; do
    [ -f "$REPO/$f" ] || die "repo_file_missing_${f//[^A-Za-z0-9]/_}"
  done
  py private-file "$ENV_FILE" || die env_test_must_be_root_0600_single_link
  if ! { [ -d "$DEPLOY_STATE" ] && py owned "$DEPLOY_STATE"; }; then die deploy_state_dir_missing; fi
  if ! { [ -d "$ETC_DIR" ] && py owned "$ETC_DIR"; }; then die etc_map_deploy_missing; fi
  [ -f "$LIB_DIR/release_manifest.py" ] || die installed_release_manifest_missing
  server=$(docker version --format '{{.Server.Version}}' 2>>"$LOG") || die docker_unavailable
  [ "${server%%.*}" -ge 28 ] 2>/dev/null || die docker_engine_28_required
  compose_version=$(docker compose version --short 2>>"$LOG") || die docker_compose_unavailable
  python3 -I -c 'import re, sys; v = tuple(int(x) for x in re.findall(r"\d+", sys.argv[1])[:3]); sys.exit(0 if v >= (2, 24, 4) else 1)' \
    "$compose_version" || die docker_compose_2_24_4_required
  PG_OPERATOR=$(env_value POSTGRES_USER)
  if ! { [[ $PG_OPERATOR =~ ^[a-z_][a-z0-9_]{0,62}$ ]] && [ "${PG_OPERATOR#map_}" = "$PG_OPERATOR" ]; }; then
    die postgres_operator_invalid
  fi
  [ "$(env_value POSTGRES_DB)" = "$DB" ] || die postgres_db_must_be_map_test
  [ "$(env_value MAP_STACK_ENV)" = test ] || die map_stack_env_must_be_test
}

load_bundle() {
  local fields
  if [ -z "$BUNDLE" ] || [ -z "$RUN_ID" ]; then die bundle_and_run_id_required; fi
  [[ $RUN_ID =~ ^[1-9][0-9]{0,19}$ ]] || die run_id_invalid
  if ! { [ -d "$BUNDLE" ] && py owned "$BUNDLE"; }; then die bundle_not_root_owned; fi
  for f in release.json compose.images.yml compose.admin-images.yml SHA256SUMS; do
    py private-file "$BUNDLE/$f" || die bundle_files_must_be_root_0600_single_link
  done
  # The registry check ties every image digest to its source commit and release tag
  # (OCI labels); SHA256SUMS alone shows only that the files belong together.
  env -i PATH="$SAFE_PATH" HOME=/root python3 -I "$LIB_DIR/release_manifest.py" verify --bundle "$BUNDLE" \
    --expected-run-id "$RUN_ID" --registry-check >>"$LOG" 2>&1 || die bundle_verification_failed
  fields=$(py bundle "$BUNDLE") || die bundle_fields_invalid
  read -r RELEASE_TAG USER_SHA USER_REF HUB_SHA HUB_REF AGENT_SHA AGENT_REF RELEASE_SHA256 <<EOF
$fields
EOF
  [ "$(env_value IMAGE_TAG)" = "$RELEASE_TAG" ] || die env_test_image_tag_must_equal_bundle_release_tag
  [ "$(env_value IMAGE_REGISTRY)" = ghcr.io/we-meet-trip ] || die env_test_image_registry_unexpected
  if [ -e "$STATE/inputs.json" ]; then
    if [ "$(py field "$STATE/inputs.json" release_json_sha256)" != "$RELEASE_SHA256" ] \
      || [ "$(py field "$STATE/inputs.json" run_id)" != "$RUN_ID" ]; then
      die bundle_differs_from_first_run
    fi
  fi
  note bundle "\"release_tag\":\"$RELEASE_TAG\",\"user_source_sha\":\"$USER_SHA\""
}

check_ci_artifact() {
  if [ -e "$STATE/inputs.json" ]; then
    CI_DIGEST=$(py field "$STATE/inputs.json" ci_artifact_digest) || die inputs_record_invalid
    return 0
  fi
  if ! { [ -n "$CI_ARTIFACT" ] && py private-file "$CI_ARTIFACT"; }; then die ci_artifact_zip_must_be_root_0600_single_link; fi
  [[ $CI_ARTIFACT_DIGEST =~ ^sha256:[a-f0-9]{64}$ ]] || die ci_artifact_digest_required
  CI_DIGEST=$(py artifact "$CI_ARTIFACT" "$CI_ARTIFACT_DIGEST" "$USER_SHA" 2>>"$LOG") || die ci_artifact_rejected
  note ci_artifact "\"digest\":\"$CI_DIGEST\""
}

sha256_of() { sha256sum <"$1" | cut -d' ' -f1; }

# Each file must match the SHA-256 that the workstation computed at the exact commit;
# the cache and SOURCES only ever hold files that matched.
fetch_one() {
  local repo=$1 sha=$2 path=$3 name=$4 id want dir part
  id="we-meet-trip/map-service-$repo@$sha:$path"
  want=$(awk -v id="$id" 'NF == 2 && $2 == id {print $1}' "$SQL_PINS")
  [[ $want =~ ^[a-f0-9]{64}$ ]] || die "sql_pin_missing_or_ambiguous_$name"
  dir=$SQL_DIR/$repo-$sha
  mkdir -p "$dir"
  if [ ! -s "$dir/$name" ]; then
    part=$dir/.$name.part
    curl -fsS --proto =https --tlsv1.2 --max-time 60 -o "$part" "$GH_RAW/map-service-$repo/$sha/$path" >>"$LOG" 2>&1 \
      || { rm -f "$part"; die "sql_fetch_failed_$name"; }
    [ "$(sha256_of "$part")" = "$want" ] || { rm -f "$part"; die "sql_pin_mismatch_$name"; }
    mv "$part" "$dir/$name"
  fi
  [ "$(sha256_of "$dir/$name")" = "$want" ] || die "sql_cache_differs_from_pin_$name"
  grep -qxF "$want  $id" "$SQL_DIR/SOURCES" 2>/dev/null || printf '%s  %s\n' "$want" "$id" >>"$SQL_DIR/SOURCES"
}

fetch_sql() {
  if ! { [ -n "$SQL_PINS" ] && py private-file "$SQL_PINS"; }; then die sql_pins_must_be_root_0600_single_link; fi
  SQL_PINS_SHA256=$(sha256_of "$SQL_PINS")
  fetch_one user "$USER_SHA" docs/user-database-bootstrap-prepare.sql user-database-bootstrap-prepare.sql
  fetch_one user "$USER_SHA" docs/user-database-bootstrap-finalize.sql user-database-bootstrap-finalize.sql
  fetch_one user "$USER_SHA" docs/user-database-bootstrap-activate.sql user-database-bootstrap-activate.sql
  fetch_one user "$USER_SHA" docs/user-database-bootstrap-quarantine.sql user-database-bootstrap-quarantine.sql
  fetch_one hub "$HUB_SHA" docs/database-roles.sql hub-database-roles.sql
  fetch_one agent "$AGENT_SHA" docs/database-roles.sql agent-database-roles.sql
  note sql_sources "\"files\":6"
}

load_secrets() {
  local line key out
  SECRETS_DIR=$(mktemp -d /run/map-db-bootstrap.XXXXXXXX)
  env -i PATH="$SAFE_PATH" HOME=/root python3 -I "$REPO/scripts/gcp_secrets.py" materialize --project "$PROJECT_ID" \
    --output "$SECRETS_DIR/materialized.env" \
    POSTGRES_PASSWORD=test-postgres-password \
    USER_DATABASE_PASSWORD=test-user-runtime-db-password \
    USER_MIGRATION_PASSWORD=test-user-migration-password \
    AGENT_DATABASE_PASSWORD=test-agent-runtime-db-password \
    HUB_DATABASE_URL=test-hub-database-url \
    HUB_MIGRATION_DATABASE_URL=test-hub-migration-database-url \
    AGENT_CHECKPOINT_MIGRATION_DSN=test-agent-checkpoint-migration-dsn \
    MAP_ADMIN_PASSWORD=test-map-admin-password \
    ADMIN_DATABASE_URL=test-admin-database-url \
    POSTGRES_EXPORTER_DSN=test-pg-exporter-dsn >>"$LOG" 2>&1 || die secret_materialize_failed
  if ! out=$(py secrets "$SECRETS_DIR/materialized.env" "$ENV_FILE" "$SECRETS_DIR" "$DB" 2>&1); then
    rm -f "$SECRETS_DIR/materialized.env"
    say "$out"
    die secrets_rejected
  fi
  rm -f "$SECRETS_DIR/materialized.env"
  say "$out"
  while IFS= read -r line; do
    key=${line%%=*}
    case $key in
      PW_USER_RUNTIME | PW_USER_MIGRATOR | PW_HUB_RUNTIME | PW_HUB_MIGRATOR | PW_AGENT_RUNTIME | PW_AGENT_MIGRATOR | PW_ADMIN | PW_EXPORTER)
        printf -v "$key" '%s' "${line#*=}" ;;
      *) die roles_file_unexpected_key ;;
    esac
  done <"$SECRETS_DIR/roles.env"
  rm -f "$SECRETS_DIR/roles.env"
}

only_postgres_in_project() {
  local id service
  for id in $(docker ps -aq --no-trunc --filter "label=com.docker.compose.project=$COMPOSE_PROJECT"); do
    service=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$id" 2>>"$LOG")
    [ "$service" = postgres ] || die "project_already_has_${service:-unknown}_container"
  done
}

preflight() {
  CURRENT_PHASE=P0_preflight
  check_host
  # First, so a run on a host whose stack already exists ends before any download or
  # secret read and holds the deployment lock only for a moment.
  only_postgres_in_project
  load_bundle
  check_ci_artifact
  fetch_sql
  load_secrets
  if [ "$CMD" = run ]; then
    [ "$APPROVE_REVOKE" = 1 ] || die owner_approval_required_for_postgis_view_revoke
    USER_IMAGE_ID=$(ensure_image "$USER_REF")
    if [ ! -e "$STATE/inputs.json" ]; then
      write_once "$STATE/inputs.json" "{\"schema\":1,\"release_tag\":\"$RELEASE_TAG\",\"run_id\":\"$RUN_ID\",\"release_json_sha256\":\"$RELEASE_SHA256\",\"user_source_sha\":\"$USER_SHA\",\"user_image\":\"$USER_REF\",\"user_image_id\":\"$USER_IMAGE_ID\",\"hub_image\":\"$HUB_REF\",\"agent_image\":\"$AGENT_REF\",\"ci_artifact_digest\":\"$CI_DIGEST\",\"sql_pins_sha256\":\"$SQL_PINS_SHA256\",\"postgis_view_revoke_approved\":true,\"created_at\":\"$(utc_now)\"}"
    fi
  fi
  note ok
}

# ---------------------------------------------------------------- P1 cluster and container mode

override_yaml() {
  cat <<EOF
# Written by the test database bootstrap. Used only until the User bootstrap is
# complete: no init scripts, no application database, health probe on postgres.
services:
  postgres:
    environment:
      POSTGRES_DB: postgres
    volumes: !override
      - postgres-data:/var/lib/postgresql/data
      - $STATE/empty-initdb:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U \${POSTGRES_USER} -d postgres"]
EOF
}

ensure_pg_mode() {
  local mode=$1 source override=$STATE/compose.postgres-bootstrap.yml
  if [ "$mode" = override ]; then
    source=$STATE/empty-initdb
    [ -z "$(ls -A "$source")" ] || die empty_initdb_dir_not_empty
    if [ -e "$override" ]; then
      [ "$(cat "$override")" = "$(override_yaml)" ] || die override_file_changed
    else
      ( set -o noclobber; override_yaml >"$override" )
    fi
    compose -f "$override" --profile infra config --format json 2>>"$LOG" \
      | py pgconfig override "$PG_OPERATOR" "$source" >>"$LOG" 2>&1 || die postgres_override_config_unexpected
    compose -f "$override" --profile infra up -d --wait --wait-timeout 240 postgres >>"$LOG" 2>&1 \
      || die postgres_start_failed
  else
    source=$REPO/db/init
    compose --profile infra config --format json 2>>"$LOG" \
      | py pgconfig normal "$PG_OPERATOR" "$source" >>"$LOG" 2>&1 || die postgres_normal_config_unexpected
    compose --profile infra up -d --wait --wait-timeout 240 postgres >>"$LOG" 2>&1 || die postgres_start_failed
  fi
  locate_pg
  [ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/docker-entrypoint-initdb.d"}}{{.Source}}{{end}}{{end}}' "$PG_ID")" = "$source" ] \
    || die postgres_mode_mismatch
  note "postgres_$mode"
}

phase_cluster() {
  CURRENT_PHASE=P1_cluster
  local mountpoint initialized=0
  if docker volume inspect "$PG_VOLUME" >/dev/null 2>&1; then
    mountpoint=$(docker volume inspect -f '{{.Mountpoint}}' "$PG_VOLUME")
    if [ -s "$mountpoint/PG_VERSION" ]; then initialized=1; fi
  fi
  if [ "$initialized" = 1 ] && [ -e "$STATE/user-bootstrap-receipt.json" ]; then
    ensure_pg_mode normal
    return 0
  fi
  if [ "$initialized" = 0 ] && [ -e "$STATE/user-bootstrap-attempt.json" ]; then die attempt_record_without_cluster; fi
  ensure_pg_mode override
  fresh_cluster_check
}

# Until the target database exists, the cluster must still be exactly what initdb made.
# This also covers a first start that stopped after initdb (a --wait timeout).
fresh_cluster_check() {
  local count
  [ ! -e "$STATE/user-bootstrap-attempt.json" ] || return 0
  count=$({ sql_quiet; sql_db_exists; } | op_sql postgres) || die database_lookup_failed
  [ "$count" = 0 ] || return 0
  expect_rows fresh_cluster_unexpected t postgres sql_fresh_cluster
  note fresh_cluster "\"postgres_image_id\":\"$(docker inspect -f '{{.Image}}' "$PG_ID")\""
}

# ---------------------------------------------------------------- P2-P6 User bootstrap

load_marker() {
  if [ -f "$STATE/marker" ]; then
    py private-file "$STATE/marker" || die marker_file_unsafe
    MARKER=$(cat "$STATE/marker")
    [[ $MARKER =~ ^[a-f0-9]{64}$ ]] || die marker_file_invalid
  fi
}

user_state() {
  local count
  count=$({ sql_quiet; sql_db_exists; } | op_sql postgres) || die database_lookup_failed
  if [ "$count" = 0 ]; then printf 'absent\n'; return 0; fi
  if [ -z "$MARKER" ]; then printf 'foreign\n'; return 0; fi
  { sql_quiet; sql_user_state; } | op_sql "$DB" || die user_state_query_failed
}

remove_job_and_network() {
  local attempt
  if [ -n "$JOB_ID" ] && docker inspect "$JOB_ID" >/dev/null 2>&1; then
    attempt=$(docker inspect -f '{{index .Config.Labels "kr.mapservice.attempt"}}' "$JOB_ID" 2>/dev/null)
    if [ "$attempt" = "$ATTEMPT_ID" ]; then
      if [ "$(docker inspect -f '{{.State.Running}}' "$JOB_ID")" = true ]; then
        docker stop --time 5 "$JOB_ID" >>"$LOG" 2>&1 || true
      fi
      docker rm "$JOB_ID" >>"$LOG" 2>&1 || true
    fi
  fi
  if docker network inspect "$BOOT_NET" >/dev/null 2>&1; then
    docker network disconnect "$BOOT_NET" "$PG_ID" >>"$LOG" 2>&1 || true
    docker network rm "$BOOT_NET" >>"$LOG" 2>&1 || true
  fi
  ! docker network inspect "$BOOT_NET" >/dev/null 2>&1 && { [ -z "$JOB_ID" ] || ! docker inspect "$JOB_ID" >/dev/null 2>&1; }
}

wait_no_bootstrap_sessions() {
  local count
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    count=$({ sql_quiet; sql_bootstrap_sessions; } | op_sql "$DB") || return 1
    [ "$count" = 0 ] && return 0
    sleep 1
  done
  return 1
}

# Prepare refuses any other backend on the new database, and an autovacuum worker can
# visit it at any moment; wait (from the postgres database) until none is attached.
wait_target_idle() {
  local count
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    count=$({ sql_quiet; sql_other_sessions; } | op_sql postgres) || return 1
    [ "$count" = 0 ] && return 0
    sleep 1
  done
  return 1
}

terminate_bootstrap_sessions() {
  local rows pid started
  rows=$({ sql_quiet; sql_bootstrap_session_inventory; } | op_sql "$DB") || return 1
  [ "$(printf '%s\n' "$rows" | grep -c .)" -le 3 ] || return 1
  while read -r pid started; do
    [ -n "$pid" ] || continue
    [[ $pid =~ ^[0-9]+$ && $started =~ ^[0-9]+$ ]] || return 1
    { sql_quiet; sql_terminate_session "$pid" "$started"; } | op_sql "$DB" >/dev/null || return 1
  done <<EOF
$rows
EOF
  wait_no_bootstrap_sessions
}

# Runs on any exit inside the one-use attempt. Never retries; preserves history,
# objects, the database and the marker for diagnosis.
quarantine_attempt() {
  local result=confirmed prepared=$PREPARED
  remove_job_and_network || result=manual_required
  if [ "$prepared" != 1 ] && [ "$({ sql_quiet; sql_bootstrap_role_exists; } | op_sql "$DB" 2>/dev/null)" = t ]; then
    prepared=1
  fi
  if [ "$prepared" = 1 ]; then
    { sql_quiet; sql_quarantine; } | op_sql "$DB" >/dev/null || result=manual_required
    terminate_bootstrap_sessions || result=manual_required
    [ "$({ sql_quiet; sql_bootstrap_retired; } | op_sql "$DB" 2>/dev/null)" = t ] || result=manual_required
  else
    result=manual_review_prepare_not_applied
  fi
  ( set -o noclobber
    printf '%s\n' "{\"status\":\"HOLD\",\"attempt_id\":\"$ATTEMPT_ID\",\"quarantine\":\"$result\",\"automatic_retry_permitted\":false,\"observed_at\":\"$(utc_now)\"}" \
      >"$STATE/user-bootstrap-failure.json" ) 2>/dev/null || true
  say "{\"phase\":\"quarantine\",\"result\":\"$result\"}"
}

boot_script() {
  printf '%s' "set -eu; IFS= read -r USER_BOOTSTRAP_PASSWORD; IFS= read -r USER_BOOTSTRAP_MARKER; USER_BOOTSTRAP_URL=jdbc:postgresql://postgres:5432/$DB; USER_BOOTSTRAP_USERNAME=map_user_bootstrap; USER_BOOTSTRAP_EXPECTED_DATABASE=$DB; export USER_BOOTSTRAP_PASSWORD USER_BOOTSTRAP_MARKER USER_BOOTSTRAP_URL USER_BOOTSTRAP_USERNAME USER_BOOTSTRAP_EXPECTED_DATABASE; unset PWD OLDPWD SHLVL _; exec /usr/bin/env -u PWD -u OLDPWD -u SHLVL -u _ java -Xmx192m -Dloader.main=map.bootstrap.UserBootstrapApplication -cp /app/app.jar org.springframework.boot.loader.launch.PropertiesLauncher bootstrap"
}

run_bootstrap_job() {
  CURRENT_PHASE=P4_bootstrap_job
  local cid output last rc state
  ! docker network inspect "$BOOT_NET" >/dev/null 2>&1 || die bootstrap_network_already_exists
  docker network create --internal --ipv6=false --opt com.docker.network.bridge.gateway_mode_ipv4=isolated \
    --label "kr.mapservice.job=$JOB_KIND" --label "kr.mapservice.project=$COMPOSE_PROJECT" "$BOOT_NET" >>"$LOG" 2>&1 \
    || die bootstrap_network_create_failed
  docker network connect --alias postgres "$BOOT_NET" "$PG_ID" >>"$LOG" 2>&1 || die bootstrap_network_connect_failed
  cid=$(docker create --pull=never --interactive --name "$COMPOSE_PROJECT-user-bootstrap-${ATTEMPT_ID:0:16}" \
    --label "kr.mapservice.job=$JOB_KIND" --label "kr.mapservice.project=$COMPOSE_PROJECT" \
    --label "kr.mapservice.attempt=$ATTEMPT_ID" --network "$BOOT_NET" --restart=no --no-healthcheck --read-only \
    --user 10001:10001 --cap-drop=ALL --security-opt=no-new-privileges:true --pids-limit=64 --memory=384m --cpus=1 \
    --log-driver=none --tmpfs /tmp:rw,noexec,nosuid,size=33554432 --entrypoint /usr/bin/timeout "$USER_REF" \
    --signal=TERM --kill-after=5s 120s /usr/bin/env -i PATH=/opt/java/openjdk/bin:/usr/bin:/bin \
    /bin/sh -c "$(boot_script)" 2>>"$LOG") || die bootstrap_job_create_failed
  [[ $cid =~ ^[a-f0-9]{64}$ ]] || die bootstrap_job_id_invalid
  JOB_ID=$cid
  write_once "$STATE/user-bootstrap-job.json" "{\"container_id\":\"$cid\",\"attempt_id\":\"$ATTEMPT_ID\"}"
  [ "$(docker inspect -f '{{.Image}}' "$cid")" = "$USER_IMAGE_ID" ] || die bootstrap_job_image_mismatch
  set +e
  # The job holds the one-use password and the marker; its stderr (JVM and driver
  # messages) is discarded, and only a strictly shaped final stdout line is kept.
  output=$(printf '%s\n%s\n' "$BOOT_PW" "$MARKER" | timeout --signal=TERM --kill-after=5 135 docker start -ai "$cid" 2>/dev/null)
  rc=$?
  set -e
  last=$(printf '%s\n' "$output" | sed '/^[[:space:]]*$/d' | tail -n 1)
  if [[ $last =~ $JOB_OUTPUT_RE ]]; then
    say "{\"phase\":\"$CURRENT_PHASE\",\"job_output\":$last}"
  fi
  [ "$rc" = 0 ] || die bootstrap_job_failed
  [ "$last" = "$BOOT_RESULT" ] || die bootstrap_job_result_unexpected
  state=$(docker inspect -f '{{.State.Running}} {{.State.ExitCode}} {{.State.OOMKilled}}' "$cid")
  [ "$state" = 'false 0 false' ] || die bootstrap_job_not_cleanly_stopped
  remove_job_and_network || die bootstrap_job_cleanup_failed
  note ok
}

user_attempt() {
  CURRENT_PHASE=P3_prepare
  [ ! -e "$STATE/user-bootstrap-attempt.json" ] || die prior_attempt_requires_manual_review
  wait_target_idle || die target_database_has_other_sessions
  ATTEMPT_ID=$(random_hex 16)
  BOOT_PW=$(random_hex 32)
  write_once "$STATE/user-bootstrap-attempt.json" "{\"status\":\"HOLD\",\"attempt_id\":\"$ATTEMPT_ID\",\"marker_sha256\":\"$(printf '%s' "$MARKER" | sha256sum | cut -d' ' -f1)\",\"user_image\":\"$USER_REF\",\"user_image_id\":\"$USER_IMAGE_ID\",\"user_source_sha\":\"$USER_SHA\",\"automatic_retry_permitted\":false,\"started_at\":\"$(utc_now)\"}"
  IN_USER_ATTEMPT=1
  apply_sql prepare_failed sql_prepare
  PREPARED=1
  note ok
  run_bootstrap_job
  CURRENT_PHASE=P5_finalize
  wait_no_bootstrap_sessions || die bootstrap_sessions_remain
  apply_sql finalize_failed sql_finalize
  IN_USER_ATTEMPT=0
  BOOT_PW=
  note ok
}

user_activation() {
  CURRENT_PHASE=P6_activate
  expect_rows scram_host_rules_required t "$DB" sql_scram_guard
  apply_sql activation_failed sql_activate
  note ok
}

user_receipt() {
  CURRENT_PHASE=P6_probes
  expect_tcp user_runtime_probe_failed map_user_runtime "$PW_USER_RUNTIME" sql_probe_user_runtime
  expect_tcp user_migrator_probe_failed map_user_migrator "$PW_USER_MIGRATOR" sql_probe_owner_via_migrator map_user_owner map_user_migrator
  expect_rows bootstrap_login_not_retired t "$DB" sql_bootstrap_retired
  expect_rows user_legacy_history_not_genuine t "$DB" sql_user_legacy_history
  if [ ! -e "$STATE/user-bootstrap-receipt.json" ]; then
    write_once "$STATE/user-bootstrap-receipt.json" "{\"schema\":1,\"status\":\"PASS\",\"database\":\"$DB\",\"marker_sha256\":\"$(printf '%s' "$MARKER" | sha256sum | cut -d' ' -f1)\",\"user_image\":\"$USER_REF\",\"user_image_id\":\"$USER_IMAGE_ID\",\"user_source_sha\":\"$USER_SHA\",\"ci_artifact_digest\":\"$CI_DIGEST\",\"migrations_executed\":4,\"legacy_checksums_verified\":true,\"bootstrap_login_retired\":true,\"runtime_migrator_scram_login\":true,\"completed_at\":\"$(utc_now)\"}"
  fi
  note ok
}

phase_user_bootstrap() {
  CURRENT_PHASE=P2_target
  local state
  load_marker
  state=$(user_state)
  case $state in
    absent)
      [ ! -e "$STATE/user-bootstrap-attempt.json" ] || die attempt_record_without_database
      if [ -z "$MARKER" ]; then
        MARKER=$(random_hex 32)
        ( set -o noclobber; printf '%s\n' "$MARKER" >"$STATE/marker" ) || die marker_write_failed
      fi
      { sql_quiet; sql_create_db; } | op_sql postgres >/dev/null || die create_database_failed
      { sql_quiet; sql_comment_db; } | op_sql postgres >/dev/null || die comment_database_failed
      state=marked
      ;;
    unmarked)
      if [ -z "$MARKER" ] || [ -e "$STATE/user-bootstrap-attempt.json" ]; then die unmarked_database_requires_review; fi
      expect_rows unmarked_database_not_pristine t "$DB" sql_pristine
      { sql_quiet; sql_comment_db; } | op_sql postgres >/dev/null || die comment_database_failed
      state=marked
      ;;
  esac
  note "$state"
  case $state in
    marked) user_attempt; user_activation; user_receipt ;;
    finalized) user_activation; user_receipt ;;
    active) user_receipt ;;
    ready | started)
      if [ -e "$STATE/user-bootstrap-failure.json" ]; then die attempt_quarantined_owner_review_required; fi
      die uncertain_attempt_run_quarantine_then_review
      ;;
    *) die target_database_not_recognized ;;
  esac
}

# ---------------------------------------------------------------- P7 User normal migrations

install_migration_env() {
  local staged=$SECRETS_DIR/$1-migration.env target=$ETC_DIR/$1-migration.env
  if [ -e "$target" ] || [ -L "$target" ]; then
    py private-file "$target" || die "${1}_migration_env_unsafe"
    cmp -s "$staged" "$target" || die "${1}_migration_env_differs_from_secret_manager"
  else
    install -o root -g root -m 0600 "$staged" "$target" || die "${1}_migration_env_install_failed"
  fi
}

service_ref() {
  case $1 in user) printf '%s\n' "$USER_REF" ;; hub) printf '%s\n' "$HUB_REF" ;; agent) printf '%s\n' "$AGENT_REF" ;; esac
}

run_migration() {
  local service=$1 operation=$2 receipt
  receipt=$STATE/receipts/$service-$operation-$(date -u +%Y%m%dT%H%M%SZ)-$(random_hex 4).json
  compose -f "$REPO/docker-compose.registry.yml" -f "$BUNDLE/compose.images.yml" --profile full config --format json 2>>"$LOG" \
    | env -i PATH="$SAFE_PATH" python3 -I "$REPO/scripts/service-migration-job.py" --service "$service" \
      --credentials "$ETC_DIR/$service-migration.env" --operation "$operation" --receipt "$receipt" >>"$LOG" 2>&1 \
    || die "${service}_${operation}_failed_see_receipt"
  py receipt "$receipt" "$service" "$operation" "$(service_ref "$service")" >>"$LOG" 2>&1 \
    || die "${service}_${operation}_receipt_invalid"
  note "$service-$operation" "\"receipt\":\"$(basename "$receipt")\""
}

# Compares the User catalog with the baseline taken after the normal User migration;
# "create" writes that baseline when it does not exist yet.
check_fingerprint() {
  local code=$1 now
  now=$({ sql_quiet; sql_user_fingerprint; } | op_sql "$DB") || die user_fingerprint_query_failed
  [[ $now =~ ^[a-f0-9]{64}$ ]] || die user_fingerprint_invalid
  if [ -e "$STATE/user-catalog.sha256" ]; then
    [ "$(cat "$STATE/user-catalog.sha256")" = "$now" ] || die "$code"
  elif [ "${2:-}" = create ]; then
    ( set -o noclobber; printf '%s\n' "$now" >"$STATE/user-catalog.sha256" )
  else
    die user_catalog_baseline_missing
  fi
}

phase_user_migrations() {
  CURRENT_PHASE=P7_user_migrations
  ensure_pg_mode normal
  CURRENT_PHASE=P7_user_migrations
  install_migration_env user
  install_migration_env hub
  install_migration_env agent
  HUB_IMAGE_ID=$(ensure_image "$HUB_REF")
  AGENT_IMAGE_ID=$(ensure_image "$AGENT_REF")
  run_migration user migrate
  run_migration user validate
  check_fingerprint user_catalog_changed_since_baseline create
  note ok
}

# ---------------------------------------------------------------- P8 provisioning

phase_provision() {
  CURRENT_PHASE=P8_provision
  local present
  # A run killed inside the first Hub migration window leaves its temporary grant;
  # withdraw it before anything checks the database.
  present=$({ sql_quiet; sql_hub_create_present; } | op_sql "$DB") || die hub_state_query_failed
  case $present in
    t)
      HUB_WINDOW_OPEN=1
      hub_scope_off || die hub_temporary_create_revocation_unconfirmed
      note hub_stale_window_revoked
      ;;
    f) ;;
    *) die hub_state_query_failed ;;
  esac
  expect_rows postgis_install_or_probe_failed t "$DB" sql_postgis
  apply_sql db_init_schemas_failed sql_file_from_repo
  apply_sql hub_role_sql_failed sql_file hub-database-roles.sql
  apply_sql agent_role_sql_failed sql_file agent-database-roles.sql
  apply_sql admin_role_sql_failed sql_admin
  apply_sql supplementary_grants_failed sql_supplementary
  apply_sql service_passwords_failed sql_service_passwords
  expect_rows provisioning_postconditions_failed t "$DB" sql_postconditions
  expect_rows user_guard_cross_schema_failed "$(printf 't\nt')" "$DB" sql_user_guard_dry_run
  check_fingerprint user_catalog_changed_by_provisioning
  note ok
}

sql_file_from_repo() { cat "$REPO/db/init/00-create-schemas.sql"; }

# ---------------------------------------------------------------- P9 Hub and Agent migrations

hub_scope_off() {
  local got
  got=$({ sql_quiet; sql_hub_scope_off; } | op_sql "$DB") || return 1
  [ "$got" = f ] || return 1
  HUB_WINDOW_OPEN=0
}

# The temporary database CREATE for the first Hub migration is granted once, on an empty
# hub_data, behind a write-once attempt record; an attempt without its closing record
# (killed run or failed migration) waits for the owner.
phase_service_migrations() {
  CURRENT_PHASE=P9_service_migrations
  local initialized got
  if [ -e "$STATE/hub-initial-window.json" ] && [ ! -e "$STATE/hub-initial-window-closed.json" ]; then
    die hub_initial_window_unfinished_owner_review
  fi
  initialized=$({ sql_quiet; sql_hub_initialized; } | op_sql "$DB") || die hub_state_query_failed
  case $initialized in t | f) ;; *) die hub_state_query_failed ;; esac
  if [ "$initialized" = f ]; then
    [ ! -e "$STATE/hub-initial-window.json" ] || die hub_initial_window_already_used
    expect_rows hub_data_not_empty_for_initial_window 0 "$DB" sql_hub_data_relations
    write_once "$STATE/hub-initial-window.json" "{\"status\":\"HOLD\",\"hub_image\":\"$HUB_REF\",\"automatic_retry_permitted\":false,\"started_at\":\"$(utc_now)\"}"
    HUB_WINDOW_OPEN=1
    got=$({ sql_quiet; sql_hub_scope_on; } | op_sql "$DB") || die hub_temporary_create_grant_failed
    [ "$got" = t ] || die hub_temporary_create_grant_not_effective
    run_migration hub migrate
    hub_scope_off || die hub_temporary_create_revocation_unconfirmed
    write_once "$STATE/hub-initial-window-closed.json" "{\"status\":\"PASS\",\"hub_image\":\"$HUB_REF\",\"closed_at\":\"$(utc_now)\"}"
    note hub_initial_window_closed
  fi
  apply_sql hub_role_sql_failed sql_file hub-database-roles.sql
  run_migration hub migrate
  apply_sql hub_role_sql_failed sql_file hub-database-roles.sql
  run_migration agent migrate
  apply_sql agent_role_sql_failed sql_file agent-database-roles.sql
  apply_sql admin_role_sql_failed sql_admin
  run_migration user validate
  check_fingerprint user_catalog_changed_by_provisioning
  note ok
}

# ---------------------------------------------------------------- verification

verify_all() {
  CURRENT_PHASE=V_verify
  expect_rows scram_host_rules_required t "$DB" sql_scram_guard
  expect_rows bootstrap_login_not_retired t "$DB" sql_bootstrap_retired
  expect_rows hub_temporary_create_present f "$DB" sql_hub_create_present
  expect_rows provisioning_postconditions_failed t "$DB" sql_postconditions
  expect_rows user_guard_cross_schema_failed "$(printf 't\nt')" "$DB" sql_user_guard_dry_run
  expect_tcp user_runtime_probe_failed map_user_runtime "$PW_USER_RUNTIME" sql_probe_user_runtime
  expect_tcp user_migrator_probe_failed map_user_migrator "$PW_USER_MIGRATOR" sql_probe_owner_via_migrator map_user_owner map_user_migrator
  expect_tcp hub_runtime_probe_failed map_hub_runtime "$PW_HUB_RUNTIME" sql_probe_service_runtime map_hub_runtime map_hub_owner hub_data alembic_version places
  expect_tcp hub_runtime_postgis_probe_failed map_hub_runtime "$PW_HUB_RUNTIME" sql_probe_hub_postgis
  expect_tcp hub_migrator_probe_failed map_hub_migrator "$PW_HUB_MIGRATOR" sql_probe_owner_via_migrator map_hub_owner map_hub_migrator
  expect_tcp agent_runtime_probe_failed map_agent_runtime "$PW_AGENT_RUNTIME" sql_probe_service_runtime map_agent_runtime map_agent_owner langgraph checkpoint_migrations checkpoints
  expect_tcp agent_migrator_probe_failed map_agent_migrator "$PW_AGENT_MIGRATOR" sql_probe_owner_via_migrator map_agent_owner map_agent_migrator
  expect_tcp admin_probe_failed map_admin "$PW_ADMIN" sql_probe_admin
  expect_tcp exporter_probe_failed map_pg_exporter "$PW_EXPORTER" sql_probe_exporter
  # Last, so every check above has already passed when a newer User release or a
  # password rotation explains a difference.
  check_fingerprint user_catalog_differs_from_bootstrap_record
  note ok
}

final_receipt() {
  CURRENT_PHASE=P9_complete
  local rows pg_image
  rows=$({ sql_quiet; sql_user_history_rows; } | op_sql "$DB") || die user_history_query_failed
  # The postgres image is pulled by tag; its ID is kept so a later move of the tag shows.
  pg_image=$(docker inspect -f '{{.Image}}' "$PG_ID" 2>>"$LOG") || die postgres_image_query_failed
  [[ $pg_image =~ ^sha256:[a-f0-9]{64}$ ]] || die image_id_invalid
  if [ ! -e "$STATE/db-bootstrap-complete.json" ]; then
    write_once "$STATE/db-bootstrap-complete.json" "{\"schema\":1,\"status\":\"DATABASE_READY_FOR_NORMAL_MIGRATIONS\",\"database\":\"$DB\",\"release_tag\":\"$RELEASE_TAG\",\"postgres_image_id\":\"$pg_image\",\"user_image_id\":\"$USER_IMAGE_ID\",\"hub_image_id\":\"$HUB_IMAGE_ID\",\"agent_image_id\":\"$AGENT_IMAGE_ID\",\"user_history_rows\":$rows,\"user_catalog_sha256\":\"$(cat "$STATE/user-catalog.sha256")\",\"public_serving\":\"not_started\",\"completed_at\":\"$(utc_now)\"}"
  fi
  note DATABASE_READY_FOR_NORMAL_MIGRATIONS "\"user_history_rows\":$rows"
}

# ---------------------------------------------------------------- commands

on_exit() {
  local rc=$?
  set +e
  if [ "$HUB_WINDOW_OPEN" = 1 ]; then
    if hub_scope_off; then
      say '{"phase":"cleanup","result":"hub_temporary_create_revoked"}'
    else
      say '{"phase":"cleanup","result":"HOLD","code":"hub_temporary_create_revocation_unconfirmed"}'
    fi
  fi
  if [ "$IN_USER_ATTEMPT" = 1 ]; then
    quarantine_attempt
  fi
  BOOT_PW=
  if [ -n "$SECRETS_DIR" ] && [ -d "$SECRETS_DIR" ]; then
    rm -rf "$SECRETS_DIR"
  fi
  exit "$rc"
}

cmd_quarantine() {
  CURRENT_PHASE=Q_quarantine
  local file
  check_host
  [ ! -e "$STATE/user-bootstrap-receipt.json" ] || die bootstrap_already_complete
  [ -e "$STATE/user-bootstrap-attempt.json" ] || die no_attempt_record
  load_marker
  [ -n "$MARKER" ] || die marker_missing
  ATTEMPT_ID=$(py field "$STATE/user-bootstrap-attempt.json" attempt_id) || die attempt_record_invalid
  USER_SHA=$(py field "$STATE/user-bootstrap-attempt.json" user_source_sha) || die attempt_record_invalid
  [[ $USER_SHA =~ ^[a-f0-9]{40}$ ]] || die attempt_record_invalid
  file=$SQL_DIR/user-$USER_SHA/user-database-bootstrap-quarantine.sql
  [ -s "$file" ] || die quarantine_sql_missing
  grep -qxF "$(sha256_of "$file")  we-meet-trip/map-service-user@$USER_SHA:docs/user-database-bootstrap-quarantine.sql" \
    "$SQL_DIR/SOURCES" || die quarantine_sql_differs_from_pinned_source
  if [ -e "$STATE/user-bootstrap-job.json" ]; then
    JOB_ID=$(py field "$STATE/user-bootstrap-job.json" container_id) || die job_record_invalid
  fi
  locate_pg
  [ "$(user_state)" != foreign ] || die target_database_not_recognized
  quarantine_attempt
}

# Re-applies the approved view revoke (same transaction as P8; the extension already
# exists) and proves the result with the provisioning postconditions and the User
# guard dry-run. It changes no container and needs no secret.
postgis_acl_restore() {
  expect_rows postgis_install_or_probe_failed t "$DB" sql_postgis
  expect_rows provisioning_postconditions_failed t "$DB" sql_postconditions
  expect_rows user_guard_cross_schema_failed "$(printf 't\nt')" "$DB" sql_user_guard_dry_run
}

cmd_postgis_acl() {
  CURRENT_PHASE=M_postgis_acl
  check_host
  [ "$APPROVE_REVOKE" = 1 ] || die owner_approval_required_for_postgis_view_revoke
  [ -e "$STATE/db-bootstrap-complete.json" ] || die bootstrap_not_complete
  locate_pg
  postgis_acl_restore
  note ok
}

# Receives, backups and the watchdog serialize on this lock; a write here must not
# overlap them.
deployment_lock() {
  local lock=$DEPLOY_STATE/deploy.lock
  if ! { [ -d "$DEPLOY_STATE" ] && py owned "$DEPLOY_STATE"; }; then die deploy_state_dir_missing; fi
  if [ -e "$lock" ] || [ -L "$lock" ]; then
    if ! { [ -f "$lock" ] && py owned "$lock"; }; then die deployment_lock_file_unsafe; fi
  fi
  exec 8>>"$lock"
  flock -w "$DEPLOY_LOCK_SECONDS" 8 || die deployment_lock_busy
}

main() {
  parse_args "$@"
  init_state
  exec 9>>"$STATE/bootstrap.lock"
  flock -n 9 || die another_bootstrap_run_active
  case $CMD in run | quarantine | postgis-acl) deployment_lock ;; esac
  trap '' HUP PIPE
  trap on_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  case $CMD in
    check)
      preflight
      ;;
    run)
      preflight
      phase_cluster
      phase_user_bootstrap
      phase_user_migrations
      phase_provision
      phase_service_migrations
      verify_all
      final_receipt
      ;;
    verify)
      CURRENT_PHASE=V_verify
      check_host
      load_secrets
      locate_pg
      verify_all
      ;;
    quarantine)
      cmd_quarantine
      ;;
    postgis-acl)
      cmd_postgis_acl
      ;;
  esac
}

if [ "${MAP_DB_BOOTSTRAP_LIBRARY:-0}" != 1 ]; then
  main "$@" </dev/null
fi
