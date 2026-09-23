# Case Management Platform

This repository is a local-first, synthetic-data case-management demo. It
uses FastAPI, SQLAlchemy, a filesystem document store, structured JSON logs,
and seeded development identities. Azure, Entra, and enterprise connectors
remain adapter seams; no corporate services are contacted by the local demo.

## Run the demo

From `backend/`, verify the local database option first:

```powershell
docker info
docker compose version
docker compose ps
```

When the Docker backend is available:

```powershell
docker compose up -d
uv sync --extra dev
uv run alembic upgrade head
uv run python -m app.cli seed
uv run uvicorn app.main:app --reload
```

If Docker is unavailable, use the SQLite test profile for local verification:

```powershell
$env:APP_ENV = "test"
$env:DATABASE_URL = "sqlite+aiosqlite:///./.pytest.db"
uv run pytest -q
```

The API is served at `http://localhost:8000`. The seed creates the synthetic
ACME tenant, intake/reviewer/supervisor identities, a published
`ServiceRequest` case type, and a demo case. In local mode, send the seeded
identity headers and an `Idempotency-Key` for mutations.

Every startup and health response identifies `SYNTHETIC ENVIRONMENT`. The
current document demo supports classified uploads, metadata/version history,
versioned redactions, document locking, and content download through the
local storage seam. The case workspace also shows participants, the document
upload widget with redaction-aware preview, and the workflow transitions
panel with guard results.

See [backend/README.md](backend/README.md) for backend commands and quality
checks, and [AGENTS.md](AGENTS.md) for the full engineering contract.
