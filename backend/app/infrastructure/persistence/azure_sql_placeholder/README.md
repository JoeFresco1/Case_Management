# INTEGRATION_SEAM: Azure SQL — placeholder for the production relational DB adapter.

This directory reserves the location for the Azure SQL implementation of every
repository port declared in `app/domain/ports.py`:

- `CaseRepository`
- `CaseTypeRepository`
- `CaseTypeVersionRepository`
- `TenantRepository`
- `AuditEventStore`
- `OutboxWriter`
- `IdempotencyStore`
- (and every other `*Repository` port as it lands)

## Status

- **Local adapter implemented**: `app/infrastructure/persistence/repositories/*`
  (SQLAlchemy 2 async + asyncpg on PostgreSQL 16).
- **Azure adapter pending**: this directory is a placeholder; the class
  below is a stub that raises `NotImplementedError` so the seam is
  *typed* and *runtime-checkable* without lying about being implemented.

## Env vars (read by the future Azure SQL adapter)

| Env var | Required when wired | Example | Purpose |
|---|---|---|---|
| `AZURE_SQL_CONNECTION_STRING` | yes (prod posture) | `mssql+aioodbc://...sql.azuresynapse.net/cases?driver=ODBC+Driver+18+for+SQL+Server` | Connection string for the Azure SQL DB. |
| `AZURE_SQL_AAD_APP_ID` | yes (managed-identity posture) | `<uuid>` | AAD app id used for managed-identity token auth. |
| `AZURE_SQL_RESOURCE` | optional | `https://database.windows.net/` | Token resource; default is sufficient for Azure SQL. |

The local placeholder never reads these variables — they exist only so the
seam location is discoverable and the env-var contract is documented in one
place.

## Protocol contract

```python
class AzureSqlCaseRepository:
    """Azure SQL adapter for CaseRepository.

    Satisfies the Protocol declared in app.domain.ports.CaseRepository.
    Implementation strategy:
      - Use SQLAlchemy 2 async + aioodbc (driver 18).
      - Auth via azure.identity.DefaultAzureCredential (managed identity) or
        AAD token + aioodbc.
      - Mirror every WHERE tenant_id = :tenant_id clause from the local
        adapter (AGENTS.md §3 R1 / R12 — defense in depth).
    """

    async def add(self, case: Case) -> None: ...  # NotImplementedError
    async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None: ...

    # ... remaining CaseRepository methods
```

## Why a placeholder instead of just a docstring

A pure docstring would let the seam drift: someone could grep the repo for
"Azure SQL" and find nothing importable. This directory gives the seam a
*location*, a *typed stub*, and a *status* that CI / future grep audits can
detect.

## Do NOT add Azure SDK imports here yet

The placeholder deliberately does **not** import `azure-identity`,
`aioodbc`, or any other Azure SDK. The synthetic environment must not drag
Azure dependencies into its dependency closure; the real adapter lives
behind a feature branch when production wiring begins.