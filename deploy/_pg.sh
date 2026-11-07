#!/usr/bin/env bash
# How the operational scripts reach PostgreSQL. Sourced, never run directly.
#
# Three deployments are supported and detected in order:
#   1. docker compose  (deploy/docker-compose.yml) — the production shape
#   2. a plain container ($PG_CONTAINER, default sagarpg-pg) — the dev shape
#   3. a local psql    ($PGHOST / $PGPORT) — a managed database
#
# DOCKER may be overridden, for example DOCKER="sudo docker" where the socket
# is root-owned.

DOCKER="${DOCKER:-docker}"
PG_USER="${POSTGRES_USER:-sagar}"
PG_DB="${POSTGRES_DB:-sagarpg}"
PG_CONTAINER="${PG_CONTAINER:-sagarpg-pg}"
COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"

_pg_mode() {
  if $DOCKER compose -f "$COMPOSE_FILE" ps -q postgres 2>/dev/null | grep -q .; then
    echo compose
  elif $DOCKER ps --format '{{.Names}}' 2>/dev/null | grep -qx "$PG_CONTAINER"; then
    echo container
  else
    echo local
  fi
}

_pg_run() {
  # psql against database $1; the remaining arguments pass straight through.
  local db="$1"; shift
  case "$(_pg_mode)" in
    compose)
      $DOCKER compose -f "$COMPOSE_FILE" exec -T postgres \
        psql -U "$PG_USER" -d "$db" "$@"
      ;;
    container)
      # -i is required: without it docker does not attach stdin, psql reads
      # nothing and exits, and the pipe feeding it dies with SIGPIPE.
      $DOCKER exec -i -e PGPASSWORD="${POSTGRES_PASSWORD:-}" \
        "$PG_CONTAINER" psql -U "$PG_USER" -d "$db" "$@"
      ;;
    local)
      PGPASSWORD="${POSTGRES_PASSWORD:-}" \
        psql -h "${PGHOST:-localhost}" -p "${PGPORT:-5432}" \
          -U "$PG_USER" -d "$db" "$@"
      ;;
  esac
}

_pg_dump() {
  local db="$1"
  case "$(_pg_mode)" in
    compose)
      $DOCKER compose -f "$COMPOSE_FILE" exec -T postgres \
        pg_dump -U "$PG_USER" -d "$db"
      ;;
    container)
      $DOCKER exec -e PGPASSWORD="${POSTGRES_PASSWORD:-}" \
        "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$db"
      ;;
    local)
      PGPASSWORD="${POSTGRES_PASSWORD:-}" \
        pg_dump -h "${PGHOST:-localhost}" -p "${PGPORT:-5432}" \
          -U "$PG_USER" -d "$db"
      ;;
  esac
}
