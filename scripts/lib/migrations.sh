#!/usr/bin/env bash
# Caller provides dc(), db_user and db_name. No host checkout of hub is needed.
#
# The upgrade itself runs in the separate bounded job, which is the only place
# that holds the migrator credential. This function reads the revision the image
# ships and the revision the database records and refuses to start applications
# when they differ. Listing heads reads the image's script directory only and
# opens no database connection, so it needs no credential.
verify_hub_revision() {
  local heads expected actual
  if ! heads=$(dc run --rm --no-deps --entrypoint alembic hub heads 2>&1); then
    echo 'hub migration: cannot read image revision' >&2
    return 1
  fi
  expected=$(printf '%s\n' "$heads" | awk '/^[[:alnum:]_]+ .*\(head\)/ {print $1}')
  if [ -z "$expected" ] || [ "$(printf '%s\n' "$expected" | wc -l | tr -d ' ')" != 1 ]; then
    echo 'hub migration: image must contain exactly one head' >&2
    return 1
  fi
  if ! actual=$(dc exec -T postgres psql -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name" -tAc \
      'select version_num from hub_data.alembic_version order by version_num'); then
    echo 'hub migration: cannot read database revision' >&2
    return 1
  fi
  actual=$(printf '%s' "$actual" | tr -d '[:space:]')
  if [ "$actual" != "$expected" ]; then
    echo 'hub migration: database revision does not match the image; startup blocked' >&2
    return 1
  fi
  echo "hub migration verified: $expected"
}
