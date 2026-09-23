"""Local system adapters (T7 diagnostics + system utilities).

# INTEGRATION_SEAM: AzureBlobDocumentStore / AzureKeyVault / AzureMonitor —
adapter contracts, replaced when corp env available.

This package holds the **infrastructure-only** side of the system
configuration surface (``specs/002-leftovers/spec.md:177``). The
application layer (``app.application.configuration.system_settings``)
stores the tenant's *declared* config; the adapters here decide what
the local process can actually *serve*:

- :class:`LocalStorageVolume` — platter/cache/volume-equivalent
  abstraction over the local filesystem root. Serves ``local-fs`` only;
  a declared ``azure-blob`` volume fails closed with
  ``DependencyUnavailable`` (never a silent local fallback that would
  pretend to be Azure durability).
- :class:`LocalEncryptionAtRest` — encryption-at-rest adapter config.
  Local posture is **plaintext at rest** (documented, development-only);
  requesting ``enabled=True`` without the KeyVault seam fails closed.
- :class:`LocalDiagnosticsService` — cache/service diagnostics: a tiny
  TTL cache with stats + clear (the cache utility behind the
  ``System Configuration`` right) plus service-status reporting derived
  from :class:`app.config.Settings`.

Azure-free by construction: no Azure SDK is imported (or required) by
this package. The production adapters will live under
``app/infrastructure/system/azure_*_placeholder/`` behind the same
shapes; the env vars they will need are listed in
``docs/integrations/seams.md``.
"""

from __future__ import annotations

__all__: list[str] = []
