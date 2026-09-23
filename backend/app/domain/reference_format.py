"""Per-tenant configurable case reference formats (FM-009 / P01-US9).

Administrators configure the **human-readable** reference pattern that the
``CaseRepository.next_reference`` adapter emits for new cases in their
tenant. The Case aggregate's internal ``id`` (UUID) is immutable across
all reference-format changes; only the on-the-wire string is affected
(Constitution I, Pillar P01 §2 — "identifiable across time and tenants").

# Why configuration is per tenant

Different tenants have different conventions. A medical deployment might
prefer ``MRN-{YYYY}-{SEQ}``; a legal deployment might prefer
``PR-REQ-{SEQ}``. The platform stores one ``ReferenceFormat`` per tenant
(an optional override; the platform default matches the legacy
``C-{tenant_short}-{YYYY}-{SEQ:06d}`` from ``app/domain/case.py``).

# Template tokens

The template is a string with a fixed token vocabulary:

- ``{TENANT}`` — the tenant's short code (uppercase alnum/dash,
  resolved by ``TenantRepository.short_key`` at format time).
- ``{TYPE}`` — the case type's stable key (uppercase alnum/underscore).
- ``{YYYY}`` — the 4-digit year of case creation (UTC).
- ``{YY}`` — the 2-digit year of case creation (UTC).
- ``{SEQ}`` — the per-tenant monotonic sequence, zero-padded to 6 digits.
- ``{SEQ:Nd}`` — same as ``{SEQ}`` but with a configurable pad width
  (``N`` between 4 and 12). Useful for high-volume tenants that need
  more than 6 digits.

Anything that is not one of those tokens is treated as a literal
character and is preserved verbatim. Backslash escaping is **not**
supported; the token vocabulary is closed and any ``{...}`` sequence
that does not match a known token is rejected at validation time.

# Why a closed token vocabulary

Unbounded templates could exfiltrate sensitive values through a chosen
prefix, embed SQL, or grow to absurd lengths. Closing the vocabulary
means: (a) no value goes into the template the operator did not
explicitly nominate, and (b) the rendered length is bounded by the
literal characters + the worst-case sequence width (``12``), which the
validator enforces.

# Existing cases are not retroactively re-numbered (Pillar P01 §6 Q1)

A format change affects only **new** cases. Existing ``cases.reference``
rows keep their original string — the internal ``Case.id`` UUID has
always been the immutable identity, and the human reference is just
one projection of it. The repository that emits references asks the
``ReferenceFormatRepository`` (or the default) for the *current*
template at the time of ``next_reference``, never replays historical
references.

The acceptance scenario for FM-009 therefore is: admin sets format
``C-{TYPE}-{YYYY}-{SEQ}``; a *new* case created afterwards uses that
format; an *existing* case still resolves with its original reference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from app.domain.errors import InvariantViolation, ValidationFailed

# Closed token vocabulary. The validator only accepts templates whose
# ``{...}`` segments match one of these names. A template that mentions
# any other identifier (e.g. ``{USERNAME}``) is rejected so an admin
# cannot smuggle sensitive data into a reference prefix.
_KNOWN_TOKENS: Final[frozenset[str]] = frozenset({"TENANT", "TYPE", "YYYY", "YY", "SEQ"})

# Allow either ``{SEQ}`` (default 6-digit pad) or ``{SEQ:N}`` with an
# explicit pad width between 4 and 12.
_SEQ_PAD_PATTERN: Final[re.Pattern[str]] = re.compile(r"^SEQ(?::(\d{1,2}))?$")

_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"\{([A-Z_:0-9]+)\}")

# Maximum rendered reference length. With the default template the
# longest realistic render is ~80 chars; 200 leaves generous headroom
# for custom literal segments while still bounding the wire size.
MAX_RENDERED_LENGTH: Final[int] = 200


@dataclass(frozen=True, slots=True)
class ReferenceFormat:
    """Per-tenant reference template.

    The template is the **human-readable** projection only. The
    ``Case.id`` UUID is unaffected by changes to this template
    (Constitution I — identity stability).

    Two fields:

    - ``template`` — the format string with the closed token
      vocabulary. Validated at construction time; an invalid template
      raises ``ValidationFailed``.
    - ``tenant_id`` — the tenant this configuration applies to. The
      store is keyed on (tenant_id); the platform default is the
      legacy ``C-{TENANT}-{YYYY}-{SEQ}`` (see
      ``platform_default_template``).
    """

    tenant_id: UUID
    template: str

    def __post_init__(self) -> None:
        validate_template(self.template)

    # ----- rendering --------------------------------------------------------

    def render(
        self,
        *,
        tenant_short: str,
        type_key: str,
        now: datetime,
        sequence: int,
    ) -> str:
        """Render this format into a concrete reference string.

        Args:
            tenant_short: the tenant's short key (uppercase alnum/dash,
                1..16 chars). Validated here so a malformed short key
                cannot poison the wire format.
            type_key: the case type's stable key (uppercase alnum/underscore,
                1..64 chars). Validated for the same reason.
            now: the case creation timestamp (UTC). Only the year is
                read; the rest is for parity with the original
                reference emission API.
            sequence: the per-tenant monotonic sequence number. Must be
                >= 0; padding is applied per the template's ``{SEQ}``
                or ``{SEQ:Nd}`` token.

        Returns:
            The rendered reference string. Always non-empty, length
            bounded by ``MAX_RENDERED_LENGTH``.

        Raises:
            InvariantViolation: ``tenant_short``, ``type_key``, or
                ``sequence`` is malformed in a way that the template
                could not render safely.
        """
        if not tenant_short or len(tenant_short) > 16:
            raise InvariantViolation(f"invalid tenant_short for reference render: {tenant_short!r}")
        if not _TENANT_SHORT_OK.fullmatch(tenant_short):
            raise InvariantViolation(f"tenant_short has invalid characters: {tenant_short!r}")
        if not type_key or len(type_key) > 64:
            raise InvariantViolation(f"invalid type_key for reference render: {type_key!r}")
        if not _TYPE_KEY_OK.fullmatch(type_key):
            raise InvariantViolation(f"type_key has invalid characters: {type_key!r}")
        if sequence < 0:
            raise InvariantViolation(f"sequence must be non-negative (got {sequence})")

        year4 = now.year
        year2 = year4 % 100

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            pad_match = _SEQ_PAD_PATTERN.fullmatch(name)
            if pad_match is not None:
                pad = int(pad_match.group(1)) if pad_match.group(1) else 6
                if pad < 4 or pad > 12:
                    raise InvariantViolation(f"SEQ pad width out of range (4..12): {pad}")
                return f"{sequence:0{pad}d}"
            if name == "TENANT":
                return tenant_short
            if name == "TYPE":
                return type_key
            if name == "YYYY":
                return f"{year4:04d}"
            if name == "YY":
                return f"{year2:02d}"
            # Token pattern only matches known names, so this branch
            # is unreachable in practice. Kept for defensive symmetry.
            raise InvariantViolation(f"unknown token: {name}")

        rendered = _TOKEN_PATTERN.sub(_replace, self.template)
        if len(rendered) > MAX_RENDERED_LENGTH:
            raise InvariantViolation(f"rendered reference exceeds {MAX_RENDERED_LENGTH} chars")
        return rendered


# ---------------------------------------------------------------------------
# Default and validation
# ---------------------------------------------------------------------------


# Platform default used when a tenant has not configured a custom
# format. Matches the legacy ``CaseReference`` format from
# ``app/domain/case.py`` so existing first-slice data is a perfect
# projection of the previous shape.
PLATFORM_DEFAULT_TEMPLATE: Final[str] = "C-{TENANT}-{YYYY}-{SEQ}"


# Tenant short key charset matches ``domain/case.py`` (uppercase alnum
# and dash, 1..16 chars).
_TENANT_SHORT_OK: Final[re.Pattern[str]] = re.compile(r"^[A-Z0-9-]{1,16}$")

# Case type keys are uppercase alnum and underscore; matches the
# CaseType key invariant in ``domain/case_type.py``.
_TYPE_KEY_OK: Final[re.Pattern[str]] = re.compile(r"^[A-Z0-9_]{1,64}$")


def platform_default_template() -> str:
    """Return the platform's built-in default reference template.

    Equivalent to ``PLATFORM_DEFAULT_TEMPLATE`` but exposed as a
    function so callers can mock it in tests without monkey-patching a
    module-level constant.
    """
    return PLATFORM_DEFAULT_TEMPLATE


def validate_template(template: str) -> None:
    """Validate a reference template string.

    A template is accepted iff:

    - It is non-empty and at most ``MAX_RENDERED_LENGTH`` characters
      long (after token substitution it must also fit — but the
      literal length is the cheap, immediate check).
    - It contains at least one ``{SEQ}`` token (a reference with no
      sequence is not a reference — it is a category).
    - Every ``{...}`` segment matches a known token from the closed
      vocabulary (``TENANT``, ``TYPE``, ``YYYY``, ``YY``, ``SEQ`` or
      ``SEQ:N`` with ``N`` in 4..12). Unknown tokens are rejected.
    - It contains no unbalanced braces.

    Raises:
        ValidationFailed: the template violates any of the above.
    """
    _validate_template_length(template)
    _validate_template_braces(template)
    _validate_template_tokens(template)


def _validate_template_length(template: str) -> None:
    if not template or not template.strip():
        raise ValidationFailed("template must be non-empty", field="template")
    if len(template) > MAX_RENDERED_LENGTH:
        raise ValidationFailed(
            f"template exceeds {MAX_RENDERED_LENGTH} chars",
            field="template",
        )


def _validate_template_braces(template: str) -> None:
    # Balanced braces. The token pattern already restricts what an
    # opening brace may contain; here we just reject any stray
    # ``{`` or ``}`` outside that vocabulary.
    depth = 0
    for ch in template:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                raise ValidationFailed(
                    "unbalanced braces in template",
                    field="template",
                )
    if depth != 0:
        raise ValidationFailed(
            "unbalanced braces in template",
            field="template",
        )


def _validate_template_tokens(template: str) -> None:
    saw_seq = False
    for match in _TOKEN_PATTERN.finditer(template):
        name = match.group(1)
        if name in _KNOWN_TOKENS:
            if name == "SEQ":
                saw_seq = True
            continue
        # ``SEQ:N`` form: still a sequence token, still satisfy the
        # "must include a sequence" rule.
        pad_match = _SEQ_PAD_PATTERN.fullmatch(name)
        if pad_match is None:
            raise ValidationFailed(
                f"unknown token {{{name}}} (allowed: {{{', '.join(sorted(_KNOWN_TOKENS))}}})",
                field="template",
            )
        if not pad_match.group(1):
            # Bare ``{SEQ}`` written as ``{SEQ:}`` with empty pad —
            # normalize, not reject.
            saw_seq = True
            continue
        pad = int(pad_match.group(1))
        if pad < 4 or pad > 12:
            raise ValidationFailed(
                f"SEQ pad width out of range (4..12): {pad}",
                field="template",
            )
        saw_seq = True

    if not saw_seq:
        raise ValidationFailed(
            "template must include a {SEQ} token",
            field="template",
        )


# ---------------------------------------------------------------------------
# Port (FM-009)
# ---------------------------------------------------------------------------


class ReferenceFormatRepository(Protocol):
    """Persistence port for per-tenant reference format overrides.

    The store is keyed on ``tenant_id``. ``upsert`` writes the new
    template atomically with the audit row in the same transaction
    (the use case boundary commits once; AGENTS.md §4.R12).

    Adapters:

    - ``InMemoryReferenceFormatRepository`` below — for tests and
      first-slice local runtime.
    - A SQL adapter (``SqlReferenceFormatRepository``) lands when the
      configuration table is added to the Alembic tree. Its signature
      will satisfy this same Protocol; the use case never imports the
      concrete class.
    """

    async def get_for_tenant(self, tenant_id: UUID) -> ReferenceFormat | None:
        """Return the tenant's override template, or ``None`` if none."""

    async def upsert(self, format: ReferenceFormat, *, now: datetime) -> None:
        """Write (or replace) the tenant's override template."""


@dataclass
class InMemoryReferenceFormatRepository:
    """Process-local adapter used by tests and the walking skeleton.

    Why dataclass + field(default_factory=dict): the dict is mutable
    shared state for the lifetime of one process. Tests construct a
    fresh instance per test; the SQL composition provider owns one
    instance for the application lifetime so local overrides survive
    requests until the durable SQL adapter lands.

    The store is intentionally NOT tenant-scoped on the lookup path:
    the dictionary is keyed on ``tenant_id`` already, so a misrouted
    call cannot leak another tenant's template.
    """

    _by_tenant: dict[UUID, ReferenceFormat] = field(default_factory=dict)

    async def get_for_tenant(self, tenant_id: UUID) -> ReferenceFormat | None:
        return self._by_tenant.get(tenant_id)

    async def upsert(self, format: ReferenceFormat, *, now: datetime) -> None:
        # ``now`` is unused; the dataclass is frozen and carries no
        # timestamp. The signature reserves the parameter for the SQL
        # adapter, which writes ``updated_at``.
        _ = now
        self._by_tenant[format.tenant_id] = format


__all__ = [
    "InMemoryReferenceFormatRepository",
    "MAX_RENDERED_LENGTH",
    "PLATFORM_DEFAULT_TEMPLATE",
    "ReferenceFormat",
    "ReferenceFormatRepository",
    "platform_default_template",
    "validate_template",
]
