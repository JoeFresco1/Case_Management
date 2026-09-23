# INTEGRATION_SEAM: Microsoft Entra ID — placeholder for the production identity-provider adapter.

This directory reserves the location for the Microsoft Entra ID
implementation of the `IdentityProvider` contract used by
`app/deps.py:get_current_user`.

## Status

- **Local adapter implemented (placeholder for Azure)**:
  `LocalHeaderIdentityProvider` (`app/deps.py`) — reads
  `X-Dev-User`/`X-Dev-Tenant` only when `APP_ENV=local`. Fails closed (401)
  on every non-local posture.
- **Azure adapter pending**: this directory is a placeholder; the class
  below is a stub that raises `NotImplementedError` so the seam is
  *typed* and *runtime-checkable* without lying about being implemented.

## Env vars (read by the future Entra adapter)

| Env var | Required when wired | Example | Purpose |
|---|---|---|---|
| `AZURE_ENTRA_TENANT_ID` | yes (prod posture) | `<tenant-guid>` | Entra tenant id. |
| `AZURE_ENTRA_CLIENT_ID` | yes (prod posture) | `<app-registration-guid>` | App registration client id. |
| `AZURE_ENTRA_AUDIENCE` | yes (prod posture) | `api://case-management` | Expected `aud` claim for issued tokens. |
| `AZURE_ENTRA_AUTHORITY` | no (derived) | `https://login.microsoftonline.com/<tenant>` | OIDC authority URL; derived from tenant id. |

The local placeholder never reads these variables — they exist only so the
seam location is discoverable and the env-var contract is documented in one
place.

## Protocol contract

```python
class EntraIdentityProvider:
    """Microsoft Entra ID adapter for the identity seam.

    Satisfies the implicit contract of ``app.deps.get_current_user``: must
    return a ``UserContext(user_id, tenant_id, roles, is_system)`` for a
    valid bearer token, and raise ``HTTPException(401)`` otherwise.

    Implementation strategy:
      - Validate the ``Authorization: Bearer ...`` JWT against the Entra
        JWKS endpoint (cached with TTL).
      - Map Entra ``tid`` -> internal ``tenant_id`` via the tenant
        registry; unknown tenants raise 401.
      - Map Entra app roles -> internal ``Action`` strings using the same
        catalog ``app.application.authorization.Action`` uses, so a role
        revoked in Entra takes effect on the very next request (live
        read, no cache — see ``RoleRepository.permissions_for_user``).
      - Never accept ``X-Dev-User`` / ``X-Dev-Tenant`` headers in any
        posture other than ``APP_ENV=local``.
    """

    async def authenticate(self, request: Request) -> UserContext:
        raise NotImplementedError("Entra adapter is not wired in this slice")
```

## Do NOT add Azure SDK imports here yet

The placeholder deliberately does **not** import `msal`, `azure-identity`,
or any other Azure SDK. The synthetic environment must not drag Azure
dependencies into its dependency closure.