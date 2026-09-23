# INTEGRATION_SEAM: Azure Blob Storage — placeholder for the production document-store adapter.

This directory reserves the location for the Azure Blob Storage implementation
of the `DocumentStore` Protocol declared in `app/domain/ports.py`.

## Status

- **Local adapter implemented**: `app/infrastructure/storage/local_document_store.py`
  (filesystem-backed, content-addressed by sha256).
- **Azure adapter pending**: this directory is a placeholder; the class
  below is a stub that raises `NotImplementedError` so the seam is
  *typed* and *runtime-checkable* without lying about being implemented.

## Env vars (read by the future Azure Blob adapter)

| Env var | Required when wired | Example | Purpose |
|---|---|---|---|
| `AZURE_BLOB_ACCOUNT_URL` | yes (prod posture) | `https://<account>.blob.core.windows.net` | Storage account endpoint. |
| `AZURE_BLOB_CONTAINER` | no (default `case-documents`) | `case-documents` | Blob container name. |
| `AZURE_BLOB_AAD_APP_ID` | optional | `<uuid>` | Managed-identity app id (uses `DefaultAzureCredential`). |
| `AZURE_BLOB_SAS_EXPIRY_SECONDS` | no (default 3600) | `3600` | SAS token expiry when SAS auth is selected. |

The local placeholder never reads these variables — they exist only so the
seam location is discoverable and the env-var contract is documented in one
place.

## Protocol contract

```python
class AzureBlobDocumentStore:
    """Azure Blob Storage adapter for DocumentStore.

    Satisfies the Protocol declared in app.domain.ports.DocumentStore.
    Implementation strategy:
      - Use azure.storage.blob.aio (BlobServiceClient) with
        DefaultAzureCredential (managed identity) in Azure-hosted runs.
      - Per-tenant container prefix: ``<root>/<tenant_id>/<sha256>``.
      - SHA-256 of the uploaded bytes is computed by the application layer
        and supplied to ``put``; the adapter stores it as metadata and
        refuses to overwrite an existing blob with a different hash.
    """

    async def put(self, *, tenant_id: UUID, content: bytes, content_type: str, sha256: str) -> str:
        raise NotImplementedError("Azure Blob adapter is not wired in this slice")

    async def get(self, *, tenant_id: UUID, content_ref: str) -> bytes:
        raise NotImplementedError("Azure Blob adapter is not wired in this slice")
```

## Do NOT add Azure SDK imports here yet

The placeholder deliberately does **not** import `azure-storage-blob` or any
other Azure SDK. The synthetic environment must not drag Azure dependencies
into its dependency closure.