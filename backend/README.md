# Case Management Platform — Backend

FastAPI + SQLAlchemy 2.x (async) + Pydantic v2 + Alembic + structlog,
managed with `uv`. Python 3.12.

## One-command startup

The documented local start sequence (AGENTS.md §7, `tasks.md:57`) — every
line is idempotent and safe to re-run from a clean checkout:

```bash
docker compose up -d                 # postgres 16 on :5432 — CLI only, no Docker Desktop
uv sync --extra dev                  # install runtime + dev deps into .venv
uv run alembic upgrade head          # apply migrations
uv run python -m app.cli seed        # ACME + 4 users + ServiceRequest case type (synthetic)
uv run uvicorn app.main:app --reload # :8000, logs "SYNTHETIC ENVIRONMENT"
```

On a host where the WSL/Docker Desktop backend is flaky (AGENTS.md §3
Issue #3), the Postgres container may fail to start even though the
`docker` CLI is available. Use SQLite for local development instead — it
needs no container and exercises the same SQLAlchemy code paths. Keep
`APP_ENV=local` for header identity; use `APP_ENV=test` for automated tests:

```bash
APP_ENV=local DATABASE_URL='sqlite+aiosqlite:///./.local.db' uv sync --extra dev
APP_ENV=local DATABASE_URL='sqlite+aiosqlite:///./.local.db' uv run alembic upgrade head
APP_ENV=local DATABASE_URL='sqlite+aiosqlite:///./.local.db' uv run python -m app.cli seed
APP_ENV=local DATABASE_URL='sqlite+aiosqlite:///./.local.db' \
    uv run uvicorn app.main:app --reload --port 8000
```

Smoke check (matches AGENTS.md §7):

```bash
curl -H "X-Dev-User: intake" \
     -H "Idempotency-Key: <uuid>" \
     -H "Content-Type: application/json" \
     -d '{"case_type_key":"ServiceRequest","title":"hello","data":{"title":"hello","priority":"normal"}}' \
     http://localhost:8000/api/v1/cases | jq .
```

`GET /api/v1/health` returns the active adapter inventory (db /
store / bus / idp / secrets / telemetry / ai_advisor / enterprise_import)
and the `synthetic_environment` flag — the UI renders the SYNTHETIC
banner whenever it is true.

## Synthetic data

The seed CLI (`app.cli.seed`, idempotent) creates one tenant (`ACME`),
four users (`intake`, `reviewer`, `supervisor`, `process_admin`), one published case type
(`ServiceRequest`), one published workflow (`ServiceRequestWorkflow`:
`intake → in_review → decided → closed`, `submit_for_review` guarded by a
required-`Contract`-document rule), `review_queue`/`intake_queue`, and one
demo case in `intake` with its workflow instance, initial `review` task,
and a 48h `review_due` SLA clock. Every email is `example.test`, no PHI is
emitted, and `SYNTHETIC ENVIRONMENT` is logged at startup (AGENTS.md §18).

The Vite dev proxy sends `X-Dev-User: intake` by default. Set
`VITE_DEV_USER=process_admin` when starting Vite to use queue and workflow
administration. The API reloads these seeded keys at startup and also
accepts their UUIDs; dev headers are rejected outside `APP_ENV=local`.

The local demo currently proves the seeded case and API foundation plus the
available document metadata/storage contract. Document upload is available
through the HTTP route; the download route delegates to the existing
application contract, while download-use-case hardening is intentionally
outside this task. Participants, the document-upload UI, and the case
transitions panel are not claimed as supported demo steps.

## Tests

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict app/domain app/application
uv run lint-imports verify
APP_ENV=test DATABASE_URL='sqlite+aiosqlite:///./.pytest.db' uv run alembic upgrade head
APP_ENV=test DATABASE_URL='sqlite+aiosqlite:///./.pytest.db' uv run pytest -q --cov
```

Coverage is required at ≥ 80% on `app/domain` + `app/application`
(`pyproject.toml:81`). Frontend types live in
`frontend/src/api/types.gen.ts` and are regenerated from
`/openapi.json` after any router change:

```bash
pnpm --prefix frontend run generate-api-types   # requires uvicorn running on :8000
```

## Layout

```
app/
  domain/            # pure dataclasses + invariants (no FastAPI / SQLAlchemy imports)
  application/       # use-case handlers + Protocol ports
  infrastructure/    # SQLAlchemy + filesystem + message-bus adapters
  interfaces/http/   # FastAPI app, routers, schemas, error model
  cli/seed.py        # synthetic dataset loader (see above)
alembic/             # 12 sequential migrations, both upgrade + downgrade
tests/               # mirrors app/ layout
```

See `../AGENTS.md` for the rule set, the seam inventory, and the
Constitution reference. See `docs/integrations/seams.md` for the Azure
adapter placeholders and how to wire a future replacement.
