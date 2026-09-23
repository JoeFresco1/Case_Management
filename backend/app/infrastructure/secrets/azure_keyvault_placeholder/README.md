# INTEGRATION_SEAM: Azure Key Vault — placeholder for the production secret-provider adapter.

This directory reserves the location for the Azure Key Vault implementation
of the `SecretProvider` Protocol declared in `app/domain/ports.py`.

## Status

- **Local adapter implemented**: `LocalSecretProvider`
  (`app/infrastructure/secrets/local_secret_provider.py`) — resolves
  `APP_SECRET_<NAME>` from the environment first, then falls back to a
  JSON file at `secrets_local_store_path`.
- **Azure adapter pending**: this directory is a placeholder; the class
  below is a stub that raises `NotImplementedError` so the seam is
  *typed* and *runtime-checkable* without lying about being implemented.

## Env vars (read by the future Key Vault adapter)

| Env var | Required when wired | Example | Purpose |
|---|---|---|---|
| `AZURE_KEY_VAULT_URL` | yes (prod posture) | `https://<vault>.vault.azure.net/` | Key Vault DNS name. |
| `AZURE_KEY_VAULT_AAD_APP_ID` | optional | `<uuid>` | Managed-identity app id (uses `DefaultAzureCredential`). |
| `SECRET_PROVIDER` | yes (selector) | `azure_key_vault` | Toggles the wiring in `app/deps.py:get_secret_provider`. Setting this without `AZURE_KEY_VAULT_URL` fails closed at startup. |

The local placeholder never reads these variables — they exist only so the
seam location is discoverable and the env-var contract is documented in one
place.

## Protocol contract

```python
class AzureKeyVaultSecretProvider:
    """Azure Key Vault adapter for SecretProvider.

    Satisfies the Protocol declared in app.domain.ports.SecretProvider.
    Implementation strategy:
      - Use azure.keyvault.secrets.aio.SecretClient with
        DefaultAzureCredential (managed identity).
      - Cache resolved secrets with a short TTL (default 60s); secret
        rotation belongs in the operator workflow, not the app, so the
        cache is intentionally short.
      - ``set_secret`` raises NotImplementedError: secret rotation
        belongs in the operator workflow, never in the application
        (AGENTS.md §12 — append-only from the application's perspective).
    """

    async def get(self, name: str) -> str:
        raise NotImplementedError("Key Vault adapter is not wired in this slice")

    async def set_secret(self, name: str, value: str) -> None:
        raise NotImplementedError(
            "Key Vault adapter is not wired in this slice — operator-managed rotation"
        )
```

## Do NOT add Azure SDK imports here yet

The placeholder deliberately does **not** import `azure-keyvault-secrets`
or any other Azure SDK. The synthetic environment must not drag Azure
dependencies into its dependency closure.