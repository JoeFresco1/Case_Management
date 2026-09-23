"""Local system adapters: storage volume, encryption-at-rest, diagnostics.

# INTEGRATION_SEAM: AzureBlobDocumentStore — adapter contract, replaced
# when corp env available.
# INTEGRATION_SEAM: AzureKeyVault field encryption — adapter contract,
# replaced when corp env available.
# INTEGRATION_SEAM: AzureMonitor / ELK / Splunk forwarding — adapter
# contract, replaced when corp env available.

Local development/test adapters for the T7 system-configuration surface
(``specs/002-leftovers/spec.md:177``). Every class here is Azure-free
(stdlib only + the local ``Settings``/``DocumentStore`` shapes) so the
synthetic environment never drags an Azure SDK into the process.

Storage choices live here — never in domain logic (AGENTS.md §12, R10).
The application layer stores the tenant's *declared* volume
(``storage.volume`` setting); :class:`LocalStorageVolume` enforces which
declarations the local process can serve and fails closed otherwise.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID

from app.domain.errors import DependencyUnavailable

#: Adapter name served by :class:`LocalStorageVolume`.
LOCAL_VOLUME_ADAPTER: Final[str] = "local-fs"

#: Adapter name reported when encryption at rest is off (local plaintext).
PLAINTEXT_ADAPTER: Final[str] = "local-plaintext"

#: Adapter name reported when encryption is requested but the KeyVault
#: seam is not wired (fail-closed placeholder, never serves bytes).
SEAM_ADAPTER: Final[str] = "azure-keyvault-seam"


class LocalStorageVolume:
    """Platter/cache/volume-equivalent abstraction over the local FS root.

    Args:
        root: base directory for document bytes (mirrors
            ``Settings.documents_path``).
        cache_mb: hot-cache budget in MiB (reported to diagnostics; the
            local adapter does not enforce eviction — enforcement is a
            production concern of the Azure tier).
        tier: ``standard`` (hot) or ``archive`` (cold). Local serves
            both labels from the same directory; the label is preserved
            so promotion/demotion policy can be authored before Azure.

    Failure mode: ``describe`` never raises (disk probing is best
    effort); ``require_adapter`` raises :class:`DependencyUnavailable`
    for anything but ``local-fs``.
    """

    def __init__(self, *, root: Path, cache_mb: int = 256, tier: str = "standard") -> None:
        self._root = Path(root)
        self._cache_mb = cache_mb
        self._tier = tier

    def describe(self) -> dict[str, object]:
        """Return the volume inventory for the diagnostics/storage view."""
        free_bytes: int | None = None
        try:
            # why: best-effort only — a mounted root that disappears
            # between probe and read must degrade the view, not 500 it.
            free_bytes = shutil.disk_usage(self._root).free
        except OSError:
            free_bytes = None
        return {
            "adapter": LOCAL_VOLUME_ADAPTER,
            "root": str(self._root),
            "cache_mb": self._cache_mb,
            "tier": self._tier,
            "free_bytes": free_bytes,
        }

    def require_adapter(self, requested: str) -> None:
        """Fail closed unless the declared volume is locally servable.

        Raises:
            DependencyUnavailable: ``requested`` is not ``local-fs``
                (e.g. ``azure-blob`` declared without the seam wired).
        """
        if requested != LOCAL_VOLUME_ADAPTER:
            raise DependencyUnavailable(
                f"storage volume {requested!r} is not available locally; "
                "active adapter is local-fs "
                "(INTEGRATION_SEAM: AzureBlobDocumentStore)"
            )


class LocalEncryptionAtRest:
    """Encryption-at-rest adapter config (local posture: plaintext).

    The local slice stores ``cases.data_json`` and document bytes in
    plaintext (see the seam note in
    ``app.infrastructure.telemetry.structured_logging``). This adapter
    makes that posture *explicit* and fails closed when the tenant
    declares ``enabled=True`` without the KeyVault seam.

    Args:
        enabled: whether the tenant declared encryption at rest on.
        key_ref: key reference id (never key content, Constitution V).
    """

    def __init__(self, *, enabled: bool = False, key_ref: str = "") -> None:
        self._enabled = enabled
        self._key_ref = key_ref

    def describe(self) -> dict[str, object]:
        """Return the encryption posture for the storage view (redact-safe)."""
        if not self._enabled:
            return {"adapter": PLAINTEXT_ADAPTER, "enabled": False, "key_ref": ""}
        # why: key_ref is a reference id, safe to surface (Constitution
        # V — values referenced by id, not content). The adapter label
        # names the seam so operators see *why* writes fail closed.
        return {"adapter": SEAM_ADAPTER, "enabled": True, "key_ref": self._key_ref}

    def wrap(self, data: bytes) -> bytes:
        """Pass bytes through (plaintext) or fail closed when enabled.

        Raises:
            DependencyUnavailable: encryption declared on without the
                KeyVault seam (production adapter missing).
        """
        if not self._enabled:
            return data
        raise DependencyUnavailable(
            "encryption at rest is declared on but the KeyVault adapter "
            "is not wired (INTEGRATION_SEAM: AzureKeyVault field encryption)"
        )

    def unwrap(self, data: bytes) -> bytes:
        """Inverse of :meth:`wrap` (same fail-closed contract)."""
        return self.wrap(data)


@dataclass
class _CacheEntry:
    """One diagnostics-cache entry with a monotonic expiry."""

    value: object
    expires_at: float


class LocalDiagnosticsService:
    """Cache/service diagnostics for the operator console (local fake).

    The cache is a small TTL map backing the ``POST
    /admin/system/cache/clear`` utility and the ``GET
    /admin/system/diagnostics`` stats. Service status reports the
    *actual* background-loop flags from :class:`app.config.Settings`
    alongside the tenant's *desired* flags from the
    ``diagnostics.services`` setting so drift is visible.

    No I/O, no Azure: all state is process-local (synthetic tenants).
    """

    def __init__(self, *, max_entries: int = 1000, ttl_seconds: int = 300) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._entries: dict[str, _CacheEntry] = {}

    def put(self, key: str, value: object) -> None:
        """Insert one entry, evicting oldest-first when over budget."""
        if len(self._entries) >= self._max_entries and key not in self._entries:
            # why: dicts preserve insertion order — popping the first
            # key is the cheapest deterministic eviction (no LRU bookkeeping
            # needed for a diagnostics cache).
            oldest = next(iter(self._entries))
            del self._entries[oldest]
        self._entries[key] = _CacheEntry(
            value=value, expires_at=time.monotonic() + self._ttl_seconds
        )

    def stats(self) -> dict[str, object]:
        """Return cache stats, lazily expiring stale entries first."""
        now = time.monotonic()
        expired = [k for k, e in self._entries.items() if e.expires_at <= now]
        for k in expired:
            del self._entries[k]
        return {
            "entries": len(self._entries),
            "max_entries": self._max_entries,
            "ttl_seconds": self._ttl_seconds,
        }

    def clear(self) -> int:
        """Drop every entry; return the number cleared (audited by caller)."""
        cleared = len(self._entries)
        self._entries.clear()
        return cleared

    def service_status(
        self,
        *,
        outbox_enabled: bool,
        sla_scanner_enabled: bool,
        webhook_dispatcher_enabled: bool,
        synthetic_environment: bool,
    ) -> dict[str, object]:
        """Report actual background-service flags + synthetic marker."""
        return {
            "outbox": outbox_enabled,
            "sla_scanner": sla_scanner_enabled,
            "webhook_dispatcher": webhook_dispatcher_enabled,
            "synthetic_environment": synthetic_environment,
        }

    def scoped_key(self, tenant_id: UUID, name: str) -> str:
        """Namespace a cache key per tenant (isolation by construction)."""
        return f"{tenant_id}:{name}"


__all__ = [
    "LOCAL_VOLUME_ADAPTER",
    "PLAINTEXT_ADAPTER",
    "SEAM_ADAPTER",
    "LocalDiagnosticsService",
    "LocalEncryptionAtRest",
    "LocalStorageVolume",
]
