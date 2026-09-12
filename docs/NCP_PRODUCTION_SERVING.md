# NCP production serving

`scripts/ncp-production-serving.py` starts the release's User, Agent, Hub,
Vision, Redis, two OSRM services and Caddy after the separate
[database installation](NCP_PRODUCTION_RECEIVER.md). It never creates or
replaces PostgreSQL, runs a migration, resets data, builds/pulls an image,
changes DNS or creates cloud resources. The production release must have
`source_ref=master`; GCP test continues to use `develop`.

`docker-compose.prod.yml` reuses the existing app environment and healthcheck
contracts. Its project is `map-prod`, network `map-prod-net`, and it contains
no PostgreSQL service or named volume. Existing PostgreSQL remains on its
original isolated migration network and is also connected to `map-prod-net`
as `postgres`. The original container ID, image ID, labels, data bind mount
and bootstrap/DB completion receipts must continue to match.

## Inputs

Keep the root-owned clean checkout at `/opt/map-service-infra` and the
existing enrollment, staged artifact/cache, `production-runtime.json` and
database receipts unchanged. The serving phase reuses the receiver's source,
host/mount and artifact checks. Every phase takes the same nonblocking
`/srv/map-prod/deploy/deploy.lock` as migration and backup.

### Serving-only controller promotion

An installed DB release pins the original infra checkout and receiver bytes.
Do not update that checkout to apply a serving-script fix. After review, CI and
owner-approved master promotion, install the clean, root-owned **master**
checkout at the fixed path `/opt/map-serving-controller`. Use the same standalone
checkout ownership rules as the original receiver (no worktree, symlink or
group/world-writable files). Record the verified master SHA and the unchanged
DB checkout SHA in root0600 `/srv/map-prod/secrets/serving-controller.json`:

```json
{
  "schema_version": 1,
  "source_ref": "master",
  "controller_source_sha": "<verified new master commit, 40 lowercase hex>",
  "database_source_sha": "<unchanged installed DB infra commit, 40 lowercase hex>"
}
```

The new controller checks its HEAD, master ref, tracked script and clean tree
against this pin. It imports the receiver and helpers, renders Compose and runs
DB Git checks from the unchanged `/opt/map-service-infra`. The original release,
cache, request and DB receipts retain their original bytes and validation.
Only serving Python changes are consumed from the new checkout; changing a
Compose file there does not alter the installed service definition.

For this installation, use `/opt/map-serving-controller/scripts/ncp-production-serving.py`
for `verify`, `start-private`, `publish` and `resume` below. A new controller pin
requires a fresh successful `start-private` before publication or resume; the
old readiness receipt cannot authorize a different controller. Preserve the
original controller's `stop-public` command as an independent emergency stop.
For systemd, install a drop-in that clears `ExecStart` and sets it to
`/usr/bin/python3 -B /opt/map-serving-controller/scripts/ncp-production-serving.py resume`.
Keep the existing `ExecStop`, working directory and mount dependencies.
Do not start/enable the unit until the new controller's private acceptance passes.

Caddy keeps the reviewed archive/index identity in the original release contract.
After the existing receiver verifies the archive, report and identity chain,
the serving controller rechecks `installations/<release>/caddy-verified.yml` with
the same installer. Its verified local immutable image ID is used consistently
for image inspection, Compose and runtime validation. Classic Docker's config ID
and containerd's index/platform IDs are supported without tagging or reloading
an image, changing the contract or bypassing its checks.

Install the following root0600 single-link files under `/srv/map-prod/secrets`:

- `production-serving.json`, using
  `deploy/ncp-bootstrap/production-serving.template.json`.
- `production-serving.env`, literal `KEY=value` lines, without shell quoting,
  variable expansion, duplicate keys or inline comments. Never shell-source
  this file or print `docker compose config` with real inputs.
- The existing `USER_DATABASE_PASSWORD`, `HUB_DATABASE_PASSWORD` and
  `AGENT_DATABASE_PASSWORD` generated during DB preparation, plus an independent
  `REDIS_PASSWORD`. Reuse those runtime values in the env/DSNs; do not copy an
  operator, bootstrap or migration credential into a serving container.

`runtime_env_sha256` pins the raw env file bytes. `gemini_key_sha256` pins
the exact UTF-8 bytes of the newly verified **map-production-api** key from
the selected `map-dev` project, without an appended newline. Both
`GEMINI_API_KEY` and `VISION_GEMINI_API_KEY` must contain that same key.
The controller never logs either value. Billing/project ownership verification
and an actual successful Gemini request are external acceptance evidence;
a matching hash alone cannot establish them.

`public_manifest_sha256` is initially `null`. Private startup and private
resume do not depend on final policy pages or Apple association data. Before
publication, set it to the raw file SHA256 of the ready public manifest.
This public-only change does not invalidate the private runtime receipt.

Required runtime identities and switches:

| Names | Required values |
|---|---|
| `APP_ENV`, `POSTGRES_DB`, `POSTGRES_HOST`, `POSTGRES_PORT` | `prod`, `map_prod`, `postgres`, `5432` |
| `USER_DATABASE_USER`, `AGENT_DATABASE_USER` | `map_user_runtime`, `map_agent_runtime` |
| `USER_DATABASE_PASSWORD`, `AGENT_DATABASE_PASSWORD`, `REDIS_PASSWORD` | Match their private files |
| `HUB_DATABASE_URL` | `postgresql+psycopg://map_hub_runtime:<URL-encoded HUB_DATABASE_PASSWORD>@postgres:5432/map_prod` |
| `REDIS_HOST`, `REDIS_PORT`, `REDIS_URL` | `redis`, `6379`, `redis://:<URL-encoded REDIS_PASSWORD>@redis:6379/0` |
| `LANGGRAPH_SCHEMA` | `langgraph` |
| `AUTH_ENFORCED`, `LOCATION_ENC_ENABLED`, `LOCATION_WIRE_ENABLED`, `APPLE_ENABLED` | `true` |
| `TESTER_SEED_ENABLED`, `PLACES_STUB_MODE`, `TRAINING_CAPTURE_ENABLED`, `TRAINING_EXPORT_ENABLED` | `false` |
| `HUB_BASE_URL`, `AGENT_BASE_URL`, `USER_SERVICE_BASE_URL` | `http://proxy:8081/hub`, `http://proxy:8081/agent`, `http://proxy:8081/user` |
| `OSRM_FOOT_BASE_URL`, `OSRM_BICYCLE_BASE_URL` | `http://osrm-foot:5000`, `http://osrm-bicycle:5000` |
| `KAKAO_PUBLIC_ORIGIN` | `https://api.mapservice.app` |
| `KAKAO_OAUTH_REDIRECT_URI` | `https://api.mapservice.app/api/v1/auth/kakao/callback` |
| `KAKAO_APP_CALLBACK_SCHEME` | `mapauth://kakao` |
| `CHAT_INVITE_BASE_URL` | `https://mapservice.app/invite/` — the final slash is required because User appends the token directly |
| `CORS_ALLOWED_ORIGINS` | `https://mapservice.app` |
| `APPLE_CLIENT_ID` | `kr.mapservice.client` |

Also supply the actual existing application inputs: `JWT_PRIVATE_KEY` and
`JWT_PUBLIC_KEY` (the User service's base64 PKCS8/X509 format),
`LOCATION_ENC_ACTIVE_KID`, `LOCATION_ENC_KEYS`, `LOCATION_WIRE_KEY`,
`CHECKPOINT_ENC_ACTIVE_KID`, `CHECKPOINT_ENC_KEYS`, four distinct
`INTERNAL_SERVICE_TOKEN`, `USER_ADMIN_INTERNAL_TOKEN`, `HUB_ADMIN_INTERNAL_TOKEN`,
`VISION_INTERNAL_TOKEN`, `GEMINI_MODEL`, `KMA_SERVICE_KEY`, `KAKAO_REST_API_KEY`,
`KAKAO_OAUTH_CLIENT_ID`, `KAKAO_OAUTH_CLIENT_SECRET`, `APPLE_TEAM_ID`,
`APPLE_KEY_ID`, `APPLE_PRIVATE_KEY_B64`, and certificate contact `EDGE_EMAIL`.
Any optional provider settings must come from the confirmed release's actual
feature configuration. The base Compose file remains the per-service allowlist.
The controller validates required presence and binding; live app readiness and
real device flows still validate credential formats and provider behavior.

## Private start and publication

Run sequentially on the reviewed NCP host:

```sh
python3 -B /opt/map-service-infra/scripts/ncp-production-serving.py verify
python3 -B /opt/map-service-infra/scripts/ncp-production-serving.py start-private
```

`verify` checks common runtime inputs/rendering and the pinned PG identity;
it does not start workloads or claim application health. `start-private`
starts the existing PG if stopped, prepares the external serving network,
and starts the eight private services. Redis is started with `--no-recreate`
to preserve its backup identity. No service auto-restarts independently.
The controller checks the actual rendered environment against each running
container, including the two Gemini consumers, before accepting readiness.

Successful private probes produce root0600 `deploy/serving-state.json` with
`PRIVATE_READY`, `public_serving=HOLD`, and runtime container/image identities.
Probes cover User health and anonymous401, Agent/Hub ready, Vision health,
and both OSRM nearest responses. The empty private
`data/proxy-upstreams` directory is intentional: `proxy/default.conf`
already supplies canonical targets and permits an empty override glob.

Build the static package with the client's `scripts/prepare-ncp-public.py
--release` from its clean `master` checkout after policy review and native
association inputs are complete. Stage its `public/` contents at
`/srv/map-prod/data/public` and its manifest separately at
`/srv/map-prod/data/public-manifest.json`; keep the manifest outside the
served tree. The controller requires `READY_FOR_PUBLICATION`, no blockers,
`source_ref=master`, a source SHA, exact file inventory/hashes, both production
domains, Android package and certificate fingerprint structure, and the actual
Apple prefix/AASA. Draft packages cannot open the edge.

After updating the public manifest pin and confirming DNS points both names
to the NCP host:

```sh
python3 -B /opt/map-service-infra/scripts/ncp-production-serving.py publish
```

Only Caddy binds public ports80/443. API routes use the existing Nginx
allowlist; public web serves policy aliases, support/deletion, invite HTML,
config and association JSON. JSON is served as `application/json` with
`no-store`; policies use `no-store`, `nosniff` and `no-referrer`. Unknown
legal/association paths do not fall through to an app page.
The first certificate may take time; the public readiness deadline is120s.
Successful public HTTP checks record `PUBLIC_READY`/`OPEN`; actual login,
chat/vision WebSockets, creation, sharing and deletion need device acceptance.

## Stop, reboot and backup acceptance

```sh
python3 -B /opt/map-service-infra/scripts/ncp-production-serving.py stop-public
python3 -B /opt/map-service-infra/scripts/ncp-production-serving.py resume
```

`stop-public` uses only the fixed `map-prod` project/service labels to stop
edge, proxy, User and Vision; missing/changed env, public files or DB receipts
cannot prevent the stop. It retains all data and the PG/Redis identities.
Explicit stop cancels publication intent, so `resume` restarts only the
private services. A failed stop records `PUBLIC_STOP_FAILED` with visibility
`UNKNOWN`, never a successful HOLD.

After private and public acceptance, install `deploy/map-prod-serving.service`
into `/etc/systemd/system`, run `systemctl daemon-reload`, and enable/start it.
Its `resume` reruns input and private checks before opening Caddy. The unit's
own graceful stop uses `--keep-resume-intent`, so a normal restart restores
the last accepted public/private state. A changed published bundle cannot
silently resume. An explicit CLI stop still prevents publication on reboot.

The existing host design requires the operator to unlock the LUKS data disk
and restore its verified mount after reboot. This unit does not install a key
or bypass that gate. After unlocking, start Docker and this service and record
same PG ID/mount, state and private/public health. Test a real reboot before
release; installing a unit is not reboot acceptance.

User8080, Agent8000 and Hub8001 are bound only to127.0.0.1 for the separate
GCP Admin SSH tunnel. Restrict that tunnel to those API ports. If the service's
trusted-CIDR input is needed, use the observed Docker gateway path; do not
add a guessed public address or expose PostgreSQL/exporter ports.

Enroll the actual PG and Redis IDs from the accepted state into the existing
`/srv/map-prod/secrets/backup.json`, with the confirmed NCP remote target and
private encryption inputs. Reuse `ncp-production-backup.py pg` and `redis`
and the existing timers, which already share `deploy.lock`. Check one real
backup of each, remote checksum/decryption and isolated restoration. This
serving controller does not automatically mark backup or recovery complete.

Local regression covers input rejection, publication ordering, emergency stop,
reboot intent, failed-close reporting, lock exclusion, and static inventory:

```sh
python3 -B -m unittest discover -s tests -p test_ncp_production_serving.py -v
```

The suite uses fixtures; it is not NCP/Linux, Caddy certificate, provider,
actual-device, store, reboot or remote recovery evidence.
