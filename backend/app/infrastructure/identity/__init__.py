"""Identity-provider adapters (AGENTS.md §12).

The local ``LocalHeaderIdentityProvider`` lives in ``app.deps`` (read-only
of ``X-Dev-User`` / ``X-Dev-Tenant`` headers, scoped to ``APP_ENV=local``).

The Microsoft Entra ID production adapter is reserved at
``app/infrastructure/identity/entra_placeholder/``.
"""
