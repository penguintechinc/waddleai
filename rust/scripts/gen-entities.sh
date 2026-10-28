#!/usr/bin/env bash
# Regenerate SeaORM entities from a live, Alembic-migrated Postgres.
# Rust never owns the schema: SQLAlchemy+Alembic stay the authority.
#   usage: gen-entities.sh [OUTPUT_DIR]   (default: crates/db/src/entities)
#   env:   TEST_DATABASE_URL  postgres URL of a migrated DB (required)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="${1:-$here/crates/db/src/entities}"
: "${TEST_DATABASE_URL:?TEST_DATABASE_URL must point at an Alembic-migrated Postgres}"
command -v sea-orm-cli >/dev/null || { echo "sea-orm-cli 2.0.4 not on PATH (cargo install sea-orm-cli@2.0.4 --locked)" >&2; exit 1; }

# Tables the data plane reads. Vector/pgvector tables are deliberately excluded.
tables=(
  organizations users teams team_members api_keys virtual_keys
  ai_providers provider_credentials routing_rules routing_rules_v2 routing_policies
  model_registry model_aliases model_access_policies model_configs
  token_conversion_rates token_usage usage_logs
  quota_org_pool quota_team_pool quota_user_reservation quota_override quota_usage_ledger
  content_filter_rules security_policies cache_configs
)

rm -rf "$out"
mkdir -p "$out"
sea-orm-cli generate entity \
  --database-url "$TEST_DATABASE_URL" \
  --database-schema public \
  --tables "$(IFS=,; echo "${tables[*]}")" \
  --date-time-crate chrono \
  --output-dir "$out"
n="$(find "$out" -name '*.rs' | wc -l)"
[ "$n" -ge "${#tables[@]}" ] || { echo "generated only $n files for ${#tables[@]} tables" >&2; exit 1; }
echo "generated $n entity files into $out"
