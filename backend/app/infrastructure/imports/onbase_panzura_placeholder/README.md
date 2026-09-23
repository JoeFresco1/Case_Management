# INTEGRATION_SEAM: OnBase / Panzura — placeholder for the production enterprise-import adapters.

This directory reserves the location for the OnBase and Panzura
implementations of the enterprise-import port (`ImportJobRepository`,
FM-026..FM-030). Both are **Gated** until Phase 3+ and have no local
adapter beyond synthetic CSV/JSONL fixtures (`app/cli/seed.py`).

## Status

- **Local adapter**: synthetic CSV/JSONL fixtures via `app/cli/seed.py`.
- **Azure / corporate adapter pending**: this directory is a placeholder;
  the classes below are stubs that raise `NotImplementedError` so the
  seam is *typed* and *runtime-checkable* without lying about being
  implemented.

## Env vars (read by the future OnBase / Panzura adapters)

| Env var | Required when wired | Example | Purpose |
|---|---|---|---|
| `ONBASE_BASE_URL` | yes (Gated) | `https://onbase.corp.example.test` | OnBase REST endpoint (placeholder; replaced with the corporate hostname at wire-up). |
| `ONBASE_DATASOURCE` | yes (Gated) | `enterprise-cases` | OnBase datasource name. |
| `ONBASE_AAD_APP_ID` | optional | `<uuid>` | Managed-identity app id for OnBase auth. |
| `ONBASE_BATCH_PAGE_SIZE` | no (default `200`) | `200` | Page size for incremental sync. |
| `PANZURA_FS_ROOT` | yes (Gated) | `https://panzura.corp.example.test/share/cases` | Panzura share root for filesystem-walk import (placeholder). |
| `PANZURA_AAD_APP_ID` | optional | `<uuid>` | Managed-identity app id for Panzura auth. |
| `PANZURA_POLL_INTERVAL_SECONDS` | no (default `300`) | `300` | Filesystem-walk poll interval. |
| `ENTERPRISE_IMPORT_ENABLED` | yes (Gated selector) | `0` | Hard-off switch. Setting to `1` without the matching `*_AAD_APP_ID` fails closed at startup. |

The local placeholder never reads these variables — they exist only so the
seam location is discoverable and the env-var contract is documented in one
place.

## Protocol contract

```python
class OnBaseImportSource:
    """OnBase adapter for ImportJobRepository.

    Satisfies the Protocol declared in app.application.imports.ports.
    Implementation strategy:
      - Use the OnBase REST API with managed-identity auth.
      - Page through the datasource with ``ONBASE_BATCH_PAGE_SIZE``;
        commit per page in the durable import-job ledger so a partial
        run can resume.
      - Map OnBase document classifications -> internal
        ``document_classifications`` declared in the tenant's CaseType
        schema (P05).
    """

    async def fetch_batch(self, *, cursor: str | None) -> list[ImportRecord]:
        raise NotImplementedError("OnBase adapter is not wired in this slice")


class PanzuraImportSource:
    """Panzura adapter for ImportJobRepository.

    Satisfies the Protocol declared in app.application.imports.ports.
    Implementation strategy:
      - Treat the Panzura share as a virtual filesystem; walk the root
        on ``PANZURA_POLL_INTERVAL_SECONDS`` and ingest only new /
        changed files (sha256 dedup).
      - Map share directory structure -> tenant / case routing per the
        operator-managed mapping table.
    """

    async def list_new_files(self, *, since: datetime | None) -> list[ImportRecord]:
        raise NotImplementedError("Panzura adapter is not wired in this slice")
```

## Do NOT add OnBase / Panzura SDK imports here yet

The placeholder deliberately does **not** import any vendor SDK. The
synthetic environment must not drag enterprise-import dependencies into
its dependency closure.