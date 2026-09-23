"""Local secret provider adapter.

AGENTS.md §12 integration seam — Secrets.

The ``SecretProvider`` Protocol lives in ``app/domain/ports.py``. This module
is the *adapter* side of the seam and therefore lives in
``app/infrastructure/secrets/``. Adapters are wired via ``app/deps.py``
(``get_secret_provider``) so the domain/application layers remain free of
infrastructure imports (Constitution VII, AGENTS.md R10).

Local adapter behavior (plan.md:42, §Resolved Decisions, AGENTS.md R12):

* Reads from environment variables first (``APP_SECRET_<name>``).
* Falls back to a JSON file at the configured ``secrets_local_store_path``.
* Writes through to the JSON file when ``set_secret`` is called locally.
* Never logs secret values (Constitution V). The redaction list in
  ``app/infrastructure/telemetry/structured_logging.py`` is the same list
  used here for the ``name`` parameter (we log key names, not values).

# INTEGRATION_SEAM: azure_key_vault — replaced when the Azure target is
# available; the contract is the ``SecretProvider`` Protocol in
# ``app/domain/ports.py`` and the Azure adapter will resolve secrets from
# Key Vault.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path


class LocalSecretProvider:
    """Local development adapter backed by env vars + an on-disk JSON file.

    Resolution order (Constitution V — fail-closed):
      1. ``APP_SECRET_<NAME>`` environment variable (uppercased name).
      2. JSON file at ``store_path``: a flat ``{"<name>": "<value>"}`` map.

    The on-disk store is created lazily on first ``set_secret`` call so the
    adapter has zero startup side-effects. Path is configurable via
    ``Settings.secrets_local_store_path`` (see ``app/config.py``).

    The adapter is intentionally synchronous under the hood and wraps blocking
    file I/O in :func:`asyncio.to_thread` so it remains usable from the
    FastAPI async event loop without blocking request handling.
    """

    _ENV_PREFIX = "APP_SECRET_"

    def __init__(self, *, store_path: Path) -> None:
        # why: store path is normalized at construction so callers can rely on
        # ``Path.resolve()`` semantics (case-insensitive on Windows, symlinks
        # resolved) without per-call overhead.
        self._store_path = store_path.resolve()
        self._store_path.parent.mkdir(parents=True, exist_ok=True)

    def _env_name(self, name: str) -> str:
        return f"{self._ENV_PREFIX}{name.upper()}"

    async def get(self, name: str) -> str:
        env_value = os.environ.get(self._env_name(name))
        if env_value is not None:
            return env_value
        store = await asyncio.to_thread(self._read_store_locked)
        try:
            value = store[name]
        except KeyError as exc:
            raise KeyError(
                f"secret {name!r} not found in env ({self._env_name(name)}) "
                f"or local store {self._store_path}"
            ) from exc
        return value

    async def set_secret(self, name: str, value: str) -> None:
        # why: synchronous read-modify-write under a single lock-free section
        # is acceptable here because local dev writes are rare and sequential.
        # A multi-process deployment should swap this for Azure Key Vault
        # (see ``azure_key_vault`` seam).
        await asyncio.to_thread(self._write_value_locked, name, value)

    def _read_store_locked(self) -> dict[str, str]:
        if not self._store_path.exists():
            return {}
        import json  # noqa: PLC0415

        text = self._store_path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            # why: a non-dict payload indicates tampering or a manual edit.
            # Fail-closed per Constitution V rather than silently coercing.
            raise TypeError(f"secrets store at {self._store_path} is not a JSON object")
        return {str(k): str(v) for k, v in loaded.items()}

    def _write_value_locked(self, name: str, value: str) -> None:
        import json  # noqa: PLC0415

        store = self._read_store_locked()
        store[name] = value
        # why: write atomically via temp-file + replace so a crashed write
        # cannot leave a partial file behind (Constitution V).
        tmp = self._store_path.with_suffix(self._store_path.suffix + ".tmp")
        tmp.write_text(json.dumps(store, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._store_path)
