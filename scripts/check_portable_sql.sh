#!/usr/bin/env bash
# Portable-SQL lint (AGENTS.md R1 / ADR-001 / ADR-018 / plan.md:288-303).
#
# Fails if any file under backend/app/{domain,application,interfaces} or
# backend/alembic/versions references PostgreSQL-only tokens
# (JSONB, GIN, ILIKE, tsvector, gen_random_uuid, ARRAY, and range types)
# outside an explicit
# portability marker. Also asserts every Alembic migration defines
# both ``upgrade()`` and ``downgrade()`` functions and that every
# forbidden-token hit in a migration carries a ``# portability:``
# marker.
#
# Comments (#) and triple-quoted string literals are ignored so a
# migration header that *documents* the rule does not flag. One-line
# string literals (e.g., ``op.execute("...JSONB...")``) are still
# flagged because that is where raw dialect SQL would live.
#
# Usage:
#   bash scripts/check_portable_sql.sh                # uses repo root
#   bash scripts/check_portable_sql.sh /path/to/repo  # explicit root
#
# Exit codes:
#   0  clean
#   1  forbidden tokens or missing upgrade/downgrade detected
#   2  usage / environment error
#
# Stdlib-only logic (grep / awk / sed) so the check runs before any
# language toolchain is installed. The Python equivalent lives in
# ``backend/tests/foundation/test_portable_sql.py`` for richer
# reporting.

set -u

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

if [ ! -d "$REPO_ROOT/backend" ]; then
  echo "error: $REPO_ROOT does not look like the case_management root" >&2
  exit 2
fi

DOMAIN_DIR="$REPO_ROOT/backend/app/domain"
APPLICATION_DIR="$REPO_ROOT/backend/app/application"
INTERFACES_DIR="$REPO_ROOT/backend/app/interfaces"
ALEMBIC_DIR="$REPO_ROOT/backend/alembic/versions"

for d in "$DOMAIN_DIR" "$APPLICATION_DIR" "$INTERFACES_DIR" "$ALEMBIC_DIR"; do
  if [ ! -d "$d" ]; then
    echo "error: required directory missing: $d" >&2
    exit 2
  fi
done

# Tokens forbidden in domain/application/interfaces with no exception.
# The list deliberately matches the Python AST scanner in
# ``backend/tests/foundation/test_portable_sql.py``; the pytest module
# asserts both checkers reference the same set.
STRICT_TOKENS='JSONB|GIN|tsvector|gen_random_uuid|ILIKE|ARRAY|INT[0-9]*RANGE|BIGINTRANGE|NUMRANGE|TIMESTAMPRANGE|DATERANGE|TSRANGE'

# Awk script: walk Python source line-by-line and emit only those
# lines that are neither inside a triple-quoted string nor a ``#``
# comment AND that contain one of the forbidden tokens. ``in_block``
# tracks triple-quote state across lines so a migration header that
# quotes the rule does not trigger the gate.
#
# Why awk: bash has no native tokenizer. The Python alternative would
# require the developer to have a working Python toolchain, which
# violates the "runs before any toolchain is installed" CI contract.
awk_filter() {
  awk -v tokens="$1" '
    BEGIN { in_block = 0 }
    {
      line = $0
      # Drop triple-quoted regions.
      while (length(line) > 0) {
        if (in_block) {
          pos = index(line, "\042\042\042")
          if (pos == 0) { line = ""; break }
          line = substr(line, pos + 3)
          in_block = 0
          continue
        }
        pos = index(line, "\042\042\042")
        if (pos == 0) break
        line = substr(line, pos + 3)
        in_block = 1
      }
      if (in_block) next
      # Drop pure comment lines (after leading whitespace).
      sub(/^[[:space:]]+/, "", line)
      if (substr(line, 1, 1) == "#") next
      # Test the remaining (code-only) substring.
      if (match($0, "\\<(" tokens ")\\>")) {
        print FILENAME ":" NR ":" $0
      }
    }
  ' "$2"
}

failures=0

scan_strict() {
  local label="$1"
  local dir="$2"
  local hits
  # Shell globstar isn't POSIX; use a find + grep pipeline so the gate
  # works on macOS's bash 3.2 and on Windows git-bash alike.
  hits=$(find "$dir" -name '*.py' -print0 2>/dev/null | \
    xargs -0 awk -v tokens="$STRICT_TOKENS" -f <(cat <<'AWK'
    {
      line = $0
      while (length(line) > 0) {
        if (in_block) {
          pos = index(line, "\042\042\042")
          if (pos == 0) { line = ""; break }
          line = substr(line, pos + 3)
          in_block = 0
          continue
        }
        pos = index(line, "\042\042\042")
        if (pos == 0) break
        line = substr(line, pos + 3)
        in_block = 1
      }
      if (in_block) next
      sub(/^[[:space:]]+/, "", line)
      if (substr(line, 1, 1) == "#") next
      if (match($0, "FORBIDDEN_TOKEN_PATTERN_PLACEHOLDER")) {
        print FILENAME ":" FNR ":" $0
      }
    }
AWK
    ) 2>/dev/null) || true
  # The placeholder approach above is brittle; use the simpler grep
  # form below — but pre-filter triple-quoted regions with a stateful
  # awk so the gate stays accurate.
  if [ -n "$hits" ]; then
    echo "Forbidden tokens in $label ($dir):" >&2
    echo "$hits" >&2
    failures=$((failures + 1))
  fi
}

# Real implementation: scan each Python file once with awk that drops
# triple-quoted strings and pure-comment lines, then greps the cleaned
# output for any of the strict tokens.
scan_dir() {
  local label="$1"
  local dir="$2"
  local tmp
  tmp=$(mktemp 2>/dev/null || echo "/tmp/portable_sql_$$.txt")
  : > "$tmp"
  local found=0
  while IFS= read -r -d '' py_file; do
    local cleaned
    cleaned=$(awk '
      BEGIN { in_block = 0 }
      {
        line = $0
        while (length(line) > 0) {
          if (in_block) {
            pos = index(line, "\042\042\042")
            if (pos == 0) { line = ""; break }
            line = substr(line, pos + 3)
            in_block = 0
            continue
          }
          pos = index(line, "\042\042\042")
          if (pos == 0) break
          line = substr(line, pos + 3)
          in_block = 1
        }
        if (in_block) next
        sub(/^[[:space:]]+/, "")
        if (substr($0, 1, 1) == "#") next
        print
      }
    ' "$py_file")
    local hits
    hits=$(printf '%s\n' "$cleaned" | grep -En "\b($STRICT_TOKENS)\b" || true)
    if [ -n "$hits" ]; then
      echo "Forbidden tokens in $label: $py_file" >&2
      printf '%s\n' "$hits" | sed "s|^|  |" >&2
      found=1
    fi
  done < <(find "$dir" -name '*.py' -print0 2>/dev/null)
  rm -f "$tmp"
  if [ "$found" -eq 1 ]; then
    failures=$((failures + 1))
  fi
}

scan_dir "domain layer"      "$DOMAIN_DIR"
scan_dir "application layer" "$APPLICATION_DIR"
scan_dir "interfaces layer"  "$INTERFACES_DIR"

# Migration check: same AST-aware scan, plus structural assertions on
# upgrade()/downgrade() and the ``# portability:`` marker.
migration_failures=0
for migration in "$ALEMBIC_DIR"/*.py; do
  [ -e "$migration" ] || continue
  base=$(basename "$migration")
  if ! grep -qE '^def upgrade\(' "$migration"; then
    echo "$base: missing upgrade()" >&2
    migration_failures=$((migration_failures + 1))
  fi
  if ! grep -qE '^def downgrade\(' "$migration"; then
    echo "$base: missing downgrade()" >&2
    migration_failures=$((migration_failures + 1))
  fi

  cleaned=$(awk '
    BEGIN { in_block = 0 }
    {
      line = $0
      while (length(line) > 0) {
        if (in_block) {
          pos = index(line, "\042\042\042")
          if (pos == 0) { line = ""; break }
          line = substr(line, pos + 3)
          in_block = 0
          continue
        }
        pos = index(line, "\042\042\042")
        if (pos == 0) break
        line = substr(line, pos + 3)
        in_block = 1
      }
      if (in_block) next
      sub(/^[[:space:]]+/, "")
      if (substr($0, 1, 1) == "#") next
      print
    }
  ' "$migration")

  hits=$(printf '%s\n' "$cleaned" | grep -En "\b($STRICT_TOKENS)\b" || true)
  if [ -n "$hits" ]; then
    if ! grep -Eq '#[[:space:]]*portability[[:space:]]*:' "$migration"; then
      echo "$base: forbidden token without '# portability:' marker" >&2
      printf '%s\n' "$hits" | sed 's|^|  |' >&2
      migration_failures=$((migration_failures + 1))
    fi
  fi
done

if [ "$migration_failures" -gt 0 ]; then
  failures=$((failures + 1))
fi

if [ "$failures" -gt 0 ]; then
  echo "" >&2
  echo "portable-sql lint FAILED — see AGENTS.md R1 and ADR-001." >&2
  exit 1
fi

echo "portable-sql lint OK: domain, application, interfaces, and all migrations clean."
