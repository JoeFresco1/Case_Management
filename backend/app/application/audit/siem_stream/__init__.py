"""SIEM stream + signed audit batches (P09-US8, US9 / FM-046 / ADR-009).

# Why a package (R2 structural split)

Formerly a single ``siem_stream.py`` module of 884 lines — past the R2
600-line soft limit. Split by responsibility into :mod:`constants`,
:mod:`types`, :mod:`helpers`, :mod:`signing`, :mod:`streaming`, and
:mod:`signed_batch`. This ``__init__`` is a pure re-export shim:
``__all__`` and every previously visible attribute are re-exported
unchanged, so no consumer import line needs editing (HTTP router
``app.interfaces.http.routers.audit``, the in-memory adapter
``app.infrastructure.persistence.siem``, and
``tests/audit/test_siem.py`` included).

# INTEGRATION_SEAM: Azure SIEM — **Gated**

The platform supports two externally-verifiable audit surfaces:

1. **Streaming** (P09-US8): a best-effort stream of persisted audit
   rows to an external SIEM. The stream is **after** the durable
   commit (the row is already in ``audit_events``), so a SIEM outage
   does not weaken the chain. The stream is replayable from the
   ``audit_events`` table — no row exists only in the SIEM.

2. **Signed batches** (P09-US9): an auditor pulls a contiguous span
   of rows that carry a signature verifiable by the auditor's
   tooling. The signature is over the canonical JSON of the rows in
   the batch; the auditor re-reads the rows through the standard
   audit read API and recomputes the hash chain (P09-US3) to confirm
   the batch matches the live durable chain.

# Why both surfaces

Streaming is for **detection** (a SIEM correlates events as they
arrive). Signed batches are for **evidence** (a regulator wants
proof that the chain is what the platform says it is, taken
off-system). They are not the same problem: streaming does not need
to be tamper-evident because the durable chain already is; signed
batches are tamper-evident *separately from* the chain so the auditor
does not have to trust the platform's read API beyond "it returns
the rows it claims to".

# Why the Gated tag

ADR-009 reserves the Azure SIEM target (vendor, signature algorithm,
key-management seam) as **Gated**. The slice ships:

- the application-layer Protocol (this package),
- an in-process streaming sink the test harness exercises,
- the HTTP entry point ``GET /api/v1/audit/signed-batches``,
- the policy action ``audit.stream`` that the four-layer deny-by-default
  authorization must grant explicitly (no role grants it today).

A concrete Azure adapter (Sentinel / ArcSight / Splunk) is a follow-up
slice that fills the seam once the vendor is selected. Until then
``SignedBatchRepository`` is satisfied by an in-memory implementation
in this package's test suite so the use case, the route, and the
acceptance tests converge without a live container.

# Stream invariant (P09-US8)

``stream_rows`` reads from the **already-persisted** ``audit_events``
table. The hash chain is built by the same ``compute_hash`` /
``verify_chain`` functions the timeline and the export use, so a SIEM
consumer that recomputes the chain from the streamed events arrives
at the same tamper-evidence verdict as an on-platform auditor. The
stream itself is best-effort: a sink error does not roll back the
commit (the row is already durable) and is recorded in telemetry.

# Signed batch invariant (P09-US9)

A signed batch covers a contiguous range of rows. The signature is
``SHA-256(secret + canonical(batch_payload))`` in v1 — HMAC-SHA-256
under a tenant-scoped key resolved through ``SecretProvider`` (AGENTS.md
§12). Production-grade asymmetric signing (RSA / ECDSA) lands with
the Azure adapter (ADR-009). The auditor verifies by:

1. Pulling the batch through the same endpoint.
2. Re-reading the covered rows through the standard audit read API
   and recomputing the chain via
   :func:`app.domain.audit.verify_chain`.
3. Recomputing the signature using the published ``key_id`` and
   ``algorithm`` and comparing against the batch's ``signature``.

The signature does not include the chain hash; the auditor verifies
the chain against the durable table and the signature against the
batch payload. Both must agree.
"""

from __future__ import annotations

from .constants import (  # noqa: F401
    ALGORITHM_HMAC_SHA256,
    ALGORITHM_LABEL,
    BATCH_KEY_ALGORITHM,
    BATCH_KEY_CREATED_AT,
    BATCH_KEY_FIRST_EVENT_ID,
    BATCH_KEY_KEY_ID,
    BATCH_KEY_LAST_EVENT_ID,
    BATCH_KEY_PAYLOAD,
    BATCH_KEY_ROW_COUNT,
    BATCH_KEY_SIGNATURE,
    BATCH_KEY_TENANT_ID,
    DEFAULT_KEY_ID,
    DEFAULT_PAGE_LIMIT,
    MAX_BATCH_ROWS,
    MAX_PAGE_LIMIT,
    Final,
)
from .helpers import (  # noqa: F401
    TypedDict,
    _ActorProxy,
    _ChainRow,
    _new_batch_id,
    _now_utc,
    _row_to_chain,
    _row_to_signed_entry,
    _row_to_stream_event,
    cast,
)
from .signed_batch import (  # noqa: F401
    BuildBatchCommand,
    CreateSignedBatchCommand,
    _load_range,
    _validate_build,
    _validate_create,
    build_signed_batch,
    create_signed_batch,
    verify_chain,
)
from .signing import (  # noqa: F401
    canonical_batch_payload,
    compute_hash,
    derive_tenant_key,
    hashlib,
    hmac,
    json,
    recompute_row_hash,
    sign_payload,
    verify_signature,
)
from .streaming import (  # noqa: F401
    Action,
    AuditEventStore,
    Clock,
    Policy,
    Resource,
    ValidationFailed,
    authorize,
    handle_stream,
)
from .types import (  # noqa: F401
    UUID,
    Any,
    Protocol,
    SiemSink,
    SignedBatch,
    SignedBatchRepository,
    StreamingResult,
    StreamQuery,
    dataclass,
    datetime,
)

__all__: list[str] = [
    "BATCH_KEY_ALGORITHM",
    "BATCH_KEY_KEY_ID",
    "BATCH_KEY_PAYLOAD",
    "BATCH_KEY_SIGNATURE",
    "DEFAULT_PAGE_LIMIT",
    "MAX_BATCH_ROWS",
    "MAX_PAGE_LIMIT",
    "SignedBatch",
    "SignedBatchRepository",
    "SiemSink",
    "StreamQuery",
    "StreamingResult",
    "build_signed_batch",
    "create_signed_batch",
    "handle_stream",
    "verify_signature",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes. Popping them keeps ``dir(app.application.audit.siem_stream)``
# identical to the old module's attribute surface (``__path__`` aside) — the
# R2 refactor must be invisible. The submodules stay importable
# (``...siem_stream.signing`` resolves through ``sys.modules``, which is
# untouched); only the redundant package-level attributes go away.
for _submodule in (
    "constants",
    "helpers",
    "signed_batch",
    "signing",
    "streaming",
    "types",
):
    globals().pop(_submodule, None)
del _submodule
