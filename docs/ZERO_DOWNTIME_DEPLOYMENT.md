# Deploying without dropping a request

The receiver used to close the public entry point for the whole private phase and
then recreate every application container in place. Both are visible outages: the
first for the length of the cutover, the second for each service's start and
health check. This branch replaces the second with a per-service replacement and
removes the need for the first.

## What changes

One service at a time, and only after its migrations have already finished:

1. A second container is created from the new image beside the one that is
   serving, on the same networks, with the same rendered environment, limits and
   read-only mounts. It publishes no host port, because the canonical container
   owns those, and it does not carry the compose service alias, so Docker's DNS
   keeps pointing at the canonical container.
2. It must pass its own health check and answer its own readiness path.
3. The proxy writes one file into a mounted override directory that re-points
   only that service's upstream variable, validates the configuration and
   reloads. A reload applies to new requests; a request already being served
   finishes against the container it started on.
4. The public probes must stay clean for a settling period.
5. Only then is the canonical container recreated from the new image. The
   temporary container is serving throughout.
6. The canonical container must be healthy and answer before the override file is
   removed and the proxy reloaded again, returning traffic to it.
7. The temporary container is stopped with a thirty second grace period and
   removed.

The order is hub, agent, vision, then the BFF, so the largest working set moves
last. Between each service the public probes must be clean.

## What a failure does

A failure at any step removes the temporary container and returns the proxy to
the canonical container. The version that was already serving keeps serving; the
deployment reports that it did not complete. Nothing is stopped in order to make
room, and no rollback of the previous release is attempted, because the previous
release never stopped. Closing the public entry point remains reserved for the
case where the serving container itself is gone.

## Requests between services

A service that calls another by its container name fails while that name is being
replaced, even though a healthy container exists. Service to service calls
therefore go through the proxy's internal listener on port 8081, which is not
published and accepts only Docker's private ranges. `HUB_BASE_URL`,
`AGENT_BASE_URL` and `USER_BASE_URL` name that listener with a `/hub`, `/agent`
or `/user` prefix; the prefix is stripped before the request is forwarded, so
callers append their usual absolute paths. The existing internal token and
trusted range checks are unchanged: the proxy's address is inside the ranges
those services already accept.

This puts the proxy on the internal path. It is a 2 MiB container whose own
replacement takes about a second, and the edge is configured to retry a refused
connection for fifteen seconds, so that second is absorbed. A request that has
already been written to an upstream is never retried, so no operation runs twice.

## Migrations

Old and new run against the same database at the same time, so a migration may
only add. `scripts/migration-expand-gate.py` reads migration files and refuses a
drop, a rename, a required column without a default, a narrowing type change or a
destructive statement. An exception must be written beside the statement as
`-- expand-gate: allow <rule> <reason>` and the reason is recorded.

The gate reads files, so each service repository runs it on its own new
migrations. The deployment enforces the other half of the contract: every
migration job finishes before any container is replaced.

## Proving it

`scripts/zd-probe.py` asks the public entry points once a second from before the
deployment starts until after it finishes: the edge health path, the application
health path, an authenticated route that must reject an anonymous caller, an
invite token that must not be found, and the actuator path that must stay closed.
An unexpected status, a refused connection and a timeout all count as failures,
and the run passes only at zero. It sends no credential and creates nothing.

A chat socket handshake can be added with `--websocket`. The socket itself is
expected to drop when the BFF container is replaced; the app reconnects within a
few hundred milliseconds. That reconnect is the accepted limit of this design:
requests do not fail, but a live socket is re-established.

## Limits

Replacing the edge itself and replacing PostgreSQL or Redis are not covered here.
The edge owns the published ports and the database owns the data directory, so
both need a different procedure and their own approval. The single host has room
for one extra container at a time, which is why services are replaced in sequence
and why each replacement first checks that free memory covers the running
container's working set with margin.
