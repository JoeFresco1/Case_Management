"""Document protection policy: restriction, versioned redaction, filing lock (FM-025 / T5).

This module is the **pure-domain** kernel for the three T5 protection rules
from ``specs/002-leftovers/spec.md`` §3.T5 and feature-map FM-025
("Authorized users restrict access, create versioned redactions, and lock
filed documents while preserving original history and auditability"):

1. **Restriction** — a document whose sensitivity is at or above
   ``restricted`` (``RESTRICTED`` / ``PHI``) requires a strictly stronger
   read grant than a normal document. A guessed-URL download without that
   grant is ``Forbidden`` with no metadata leak (no file name, storage
   ref, or content in the error details).
2. **Versioned redaction (original preserved)** — a redaction never mutates
   the parent ``Document`` aggregate or its ``DocumentVersion`` rows. The
   redacted bytes land in a separate ``DocumentRedaction`` row and the
   *original* bytes stay retrievable only under the stricter right, even
   when the parent document itself is merely ``internal`` (the original
   may contain exactly the bytes that were redacted away).
3. **Filing lock** — once ``Document.locked`` is set (the "filed" state),
   every mutating operation (new version, redaction, restriction change,
   deletion) is refused with ``DocumentLocked`` before any side effect.

# Why a separate module instead of methods on ``Document``

``app.domain.document`` owns the aggregates and their state transitions
(``with_new_version``, ``with_lock``, ``DocumentRedaction.create``). This
module owns the *policy* questions the application layer asks before
touching storage: "which permission does this read need?" and "is this
mutation allowed at all?". Keeping policy separate means the rules are
unit-testable without repositories, clocks, or async harnesses, and the
application use cases (``protect_document``, ``download_document``,
``version_document``) share one implementation instead of re-deriving the
threshold inline.

# INTEGRATION_SEAM: future ``document.read_restricted`` action

The stricter permission is named here as the string
``document.read_restricted``. The application ``Action`` enum does not bind
it yet; when it does, ``protect_document`` / ``download_document`` will map
this module's requirement onto the live role grant. Until then the string
is the contract the tests pin.
"""

from __future__ import annotations

from uuid import UUID

from app.domain.document import Document, DocumentSensitivity
from app.domain.errors import DocumentLocked, Forbidden, ValidationFailed

# ---------------------------------------------------------------------------
# Permission vocabulary (plain strings — the domain must not import the
# application ``Action`` enum per the layer contract).
# ---------------------------------------------------------------------------

#: Base right: reading a normal document or a redacted view.
READ_PERMISSION = "document.read"

#: Stricter right: reading a restricted document or any original
#: (pre-redaction) bytes. Never granted implicitly by ``READ_PERMISSION``.
READ_RESTRICTED_PERMISSION = "document.read_restricted"

#: Sensitivities at or above the restriction threshold. Anything in this
#: set requires ``READ_RESTRICTED_PERMISSION`` even for the redacted view.
RESTRICTED_SENSITIVITIES: frozenset[DocumentSensitivity] = frozenset(
    {DocumentSensitivity.RESTRICTED, DocumentSensitivity.PHI}
)

# Safe, metadata-free reason codes for ``Forbidden.details``. The details
# mapping must never carry file names, storage refs, or content — a
# guessed-URL probe must learn nothing beyond "forbidden".
_REASON_RESTRICTED = "restricted_document"
_REASON_ORIGINAL = "original_requires_stricter_right"
_REASON_CROSS_TENANT = "cross_tenant"

_MAX_REASON_CHARS = 500
_MAX_REGION_CHARS = 500


def is_access_restricted(document: Document) -> bool:
    """Return True when ``document`` requires the stricter read right.

    Args:
        document: The document aggregate to classify.

    Returns:
        True iff ``document.sensitivity`` is ``restricted`` or ``phi``.
        All other levels (``public`` / ``internal`` / ``confidential``)
        use the base ``document.read`` grant.

    Failure mode: pure predicate; never raises.
    """
    return document.sensitivity in RESTRICTED_SENSITIVITIES


def required_read_permission(document: Document, *, for_original: bool) -> str:
    """Return the permission string needed to read ``document``.

    Args:
        document: The document aggregate being read.
        for_original: True when the caller wants the original
            (pre-redaction) bytes rather than the current/redacted view.

    Returns:
        ``READ_RESTRICTED_PERMISSION`` when ``for_original`` is True (the
        original is always under the stricter right — it may contain
        exactly the bytes a redaction removed) or when the document
        itself is access-restricted; otherwise ``READ_PERMISSION``.

    Failure mode: pure function; never raises.
    """
    if for_original:
        # Why unconditional: a redaction removes sensitive bytes from the
        # *view* but the original row still holds them, so the original
        # must stay under the stricter right even for an ``internal``
        # parent. Otherwise redaction would be a no-op for confidentiality.
        return READ_RESTRICTED_PERMISSION
    if is_access_restricted(document):
        return READ_RESTRICTED_PERMISSION
    return READ_PERMISSION


def check_read_allowed(
    *,
    document: Document,
    actor_tenant_id: UUID,
    granted_permissions: frozenset[str],
    for_original: bool = False,
) -> None:
    """Enforce the read-access rule for one document.

    Args:
        document: The document aggregate being read. Its own
            ``tenant_id`` is the authority for the tenant comparison.
        actor_tenant_id: The tenant the actor is acting in.
        granted_permissions: The actor's live permission union (already
            resolved from role assignments by the application layer).
        for_original: True when the caller wants original
            (pre-redaction) bytes; the stricter right is then required
            regardless of the document's own sensitivity.

    Returns:
        None on success (the read may proceed).

    Raises:
        Forbidden: On cross-tenant access (even with the grant — tenant
            isolation is deny-by-default) or when the required permission
            is absent. ``details`` carries only a safe ``reason`` code;
            it never echoes file names, storage refs, content, or sizes
            so a guessed-URL probe learns nothing (T5 acceptance).
    """
    if actor_tenant_id != document.tenant_id:
        # Why first: cross-tenant is denied unconditionally, on top of
        # ``WHERE tenant_id = :tenant_id`` (Constitution V, FM-002).
        raise Forbidden(
            "cross-tenant document access denied",
            details={"reason": _REASON_CROSS_TENANT},
        )
    required = required_read_permission(document, for_original=for_original)
    if required not in granted_permissions:
        reason = _REASON_ORIGINAL if for_original else _REASON_RESTRICTED
        # Why no document metadata in details: the caller may be
        # guessing URLs; confirming file names/sizes would leak
        # existence beyond the bare forbidden signal.
        raise Forbidden(
            "document read denied",
            details={"reason": reason},
        )


def check_mutation_allowed(document: Document) -> None:
    """Enforce the filing-lock rule: a filed (locked) document is immutable.

    Args:
        document: The document aggregate the caller intends to mutate
            (new version, redaction, restriction change, or deletion).

    Returns:
        None when the document is unlocked (the mutation may proceed to
        its own validation).

    Raises:
        DocumentLocked: When ``document.locked`` is True. The error
            surfaces before any bytes are stored or any row is written
            so a filed document cannot be silently overwritten. Maps to
            HTTP 423 Locked at the interface boundary.
    """
    if document.locked:
        raise DocumentLocked(f"document {document.id} is filed; mutations are refused")


def validate_redaction(
    document: Document,
    *,
    base_version_number: int,
    reason: str,
    region: str | None,
) -> None:
    """Validate a versioned-redaction request without performing I/O.

    The redaction itself (hashing bytes, inserting the
    ``DocumentRedaction`` row, superseding the prior ACTIVE row, audit +
    outbox) stays in the application use case; this function is the pure
    gate that runs first so invalid input fails before any side effect.

    Args:
        document: The parent document aggregate (provides ``locked`` and
            ``current_version`` for the range check).
        base_version_number: The document version the redaction is
            derived from; must satisfy ``1 <= n <= current_version`` so
            the audit can prove "redacted from vN" against real history.
        reason: Free-text justification, 1..500 chars after stripping.
            Reproduced in the audit payload, so it must never carry PII
            or secrets (callers enforce that at the boundary).
        region: Optional region description (page/paragraph/box), at most
            500 chars when present.

    Returns:
        None when the request is well-formed.

    Raises:
        DocumentLocked: When the document is filed (checked first — a
            locked document refuses even a well-formed redaction).
        ValidationFailed: With ``field`` set to ``base_version_number``,
            ``reason``, or ``region`` for the corresponding violation.
    """
    # Why lock first: the filing-lock refusal must hold regardless of
    # whether the rest of the request is valid, so callers get one
    # deterministic refusal instead of a validation error that masks it.
    check_mutation_allowed(document)
    if base_version_number < 1 or base_version_number > document.current_version:
        raise ValidationFailed(
            f"base_version_number {base_version_number} is not in the "
            f"document's version history (1..{document.current_version})",
            field="base_version_number",
        )
    if not reason or not reason.strip() or len(reason.strip()) > _MAX_REASON_CHARS:
        raise ValidationFailed(
            "redaction reason is required (1..500 chars)",
            field="reason",
        )
    if region is not None and len(region) > _MAX_REGION_CHARS:
        raise ValidationFailed(
            "redaction region must be <= 500 chars",
            field="region",
        )


__all__ = [
    "READ_PERMISSION",
    "READ_RESTRICTED_PERMISSION",
    "RESTRICTED_SENSITIVITIES",
    "check_mutation_allowed",
    "check_read_allowed",
    "is_access_restricted",
    "required_read_permission",
    "validate_redaction",
]
