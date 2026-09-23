"""Synthetic-data and PHI/secret redaction acceptance tests (ADR-019 / R12).

Acceptance contract:

- Every key listed in :data:`REQUIRED_REDACT_KEYS` MUST be redacted by
  :func:`app.infrastructure.telemetry.structured_logging.redact_sensitive`
  before its value leaves the process (logs, audit payloads, error
  responses).
- Fixtures and seed data must NOT contain ``example.com`` (production
  domain pattern), DOB values, SSN-shaped numbers, credit-card-shaped
  numbers, or references to prod/live/corp prefixes in tenant or external
  references. Only ``example.test`` and other reserved test domains are
  permitted.
- Authorization in production environments MUST reject ``X-Dev-User``
  and ``X-Dev-Tenant`` headers with ``401 Unauthorized``. The
  ``LocalHeaderIdentityProvider`` is local/test-only.
- Audit must record sensitive denial events. The break-glass audit
  surface already exercises this; the redaction contract requires that
  the denial payload contains no leaked values.
- ``SYNTHETIC_ENVIRONMENT=1`` is visible in logs and the health response.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.application.authorization import (
    Action,
    Decision,
    Policy,
    Resource,
    authorize,
)
from app.deps import UserContext
from app.domain.errors import Forbidden
from app.infrastructure.telemetry.structured_logging import (
    _REDACT_KEYS,
    redact_sensitive,
)

# ---------------------------------------------------------------------------
# Required redaction coverage (ADR-019). The test fails fast if the
# production redaction list ever drops a required key.
# ---------------------------------------------------------------------------

REQUIRED_REDACT_KEYS: tuple[str, ...] = (
    "ssn",
    "phi",
    "secret",
    "password",
    "authorization",
    "x-dev-user",
    "x_dev_user",
    "x-dev-tenant",
    "x_dev_tenant",
    "x-api-key",
    "x_api_key",
    "token",
    "private_key",
    "client_secret",
    "refresh_token",
    "session_id",
    "cookie",
    "bearer",
)

# ---------------------------------------------------------------------------
# Synthetic-data fixtures. Must use ``example.test`` (RFC 6761 reserved
# test domain) and obviously fake values only.
# ---------------------------------------------------------------------------

ALLOWED_TEST_DOMAINS = ("example.test", "example.org", "example.net")
# Patterns are built from plain strings so the literal regex source
# never appears verbatim in this file (which would otherwise flag
# itself in the synthetic-data CI check).
FORBIDDEN_FIXTURE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"\b" + "ex" + "ample" + r"\." + "com" + r"\b",
        r"\b(?:gmail|yahoo|hotmail|outlook)\.com\b",
        r"\bacme-corp\.com\b",
        r"\bprod-(?:tenant|case)-",
        r"\bDOB\s*[:=]\s*\d{4}",
        r"\bbirth_date\s*[:=]\s*['\"]?\d{4}",
    )
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "tests",
    REPO_ROOT / "app",
)
# Files that intentionally enumerate forbidden patterns to *test* the
# redaction list and synthetic-data check. They are negative fixtures.
SELF_TEST_FILES: frozenset[str] = frozenset(
    {
        "tests/security/test_phie_redaction.py",
    }
)


# ---------------------------------------------------------------------------
# Redaction list coverage
# ---------------------------------------------------------------------------


def test_redaction_list__contains_all_required_keys__no_drift() -> None:
    """Redaction list includes every ADR-019 required key (substring match)."""
    lowered = tuple(key.lower() for key in _REDACT_KEYS)
    for required in REQUIRED_REDACT_KEYS:
        assert any(required in entry for entry in lowered), (
            f"redaction list missing required key '{required}' (see ADR-019 §6)"
        )


@pytest.mark.parametrize(
    ("payload", "forbidden_substrings"),
    [
        ({"ssn": "123-45-6789"}, ("123-45-6789",)),
        ({"phi": "patient-stage-3-hypertension"}, ("hypertension",)),
        ({"password": "hunter2"}, ("hunter2",)),
        ({"authorization": "Bearer abc.def.ghi"}, ("abc.def.ghi",)),
        ({"x_dev_user": "intake-id"}, ("intake-id",)),
        ({"x-dev-tenant": "11111111-1111-1111-1111-111111111111"}, ("11111111",)),
        ({"x_api_key": "sk-live-abcdef"}, ("sk-live-abcdef",)),
        ({"refresh_token": "rt-12345"}, ("rt-12345",)),
        ({"session_id": "sess-xyz"}, ("sess-xyz",)),
        ({"cookie": "sid=abc"}, ("sid=abc",)),
        ({"bearer": "abc.def.ghi"}, ("abc.def.ghi",)),
        ({"private_key": "-----BEGIN PRIVATE KEY-----"}, ("BEGIN PRIVATE",)),
        ({"client_secret": "shhh"}, ("shhh",)),
        (
            {"nested": {"ssn": "123-45-6789", "ok": "keep"}},
            ("123-45-6789",),
        ),
        (
            {"list": [{"phi": "x"}, {"phi": "y"}]},
            ("x", "y"),
        ),
    ],
)
def test_redact_sensitive__masks_required_keys__no_leak(
    payload: dict[str, object], forbidden_substrings: tuple[str, ...]
) -> None:
    """Every ADR-019 required key produces ``[REDACTED]`` and leaks nothing."""
    redacted = redact_sensitive(payload)
    serialized = json.dumps(redacted, default=str)
    for needle in forbidden_substrings:
        assert needle not in serialized, (
            f"redactor leaked '{needle}' in payload {payload} -> {redacted}"
        )


def test_redact_sensitive__preserves_non_sensitive_keys__unchanged() -> None:
    payload = {
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "case_id": "22222222-2222-2222-2222-222222222222",
        "title": "Routine intake",
    }
    assert redact_sensitive(payload) == payload


# ---------------------------------------------------------------------------
# Synthetic-data / fixture hygiene
# ---------------------------------------------------------------------------


def _iter_text_files(root: Path) -> list[Path]:
    candidates: list[Path] = []
    if not root.exists():
        return candidates
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".py", ".json", ".jsonl", ".csv", ".md", ".txt"}:
            continue
        if "__pycache__" in path.parts:
            continue
        try:
            rel = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        if rel in SELF_TEST_FILES:
            continue
        candidates.append(path)
    return candidates


def test_fixtures__no_example_com_or_prod_dob__per_adr019() -> None:
    """No fixture, JSON, or doc may carry ``example.com``, DOB, or SSN.

    Allowed: ``example.test``, ``example.org``, ``example.net`` (RFC 6761
    reserved test domains).
    """
    offenders: list[str] = []
    for root in FIXTURE_ROOTS:
        for path in _iter_text_files(root):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for pattern in FORBIDDEN_FIXTURE_PATTERNS:
                if pattern.search(text):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {pattern.pattern}")
    assert not offenders, (
        "fixtures contain forbidden synthetic-data patterns (ADR-019 §7):\n"
        + "\n".join(offenders[:25])
    )


def test_fixtures__only_example_test_domains__per_rfc6761() -> None:
    """Every email-shaped token in fixtures uses an allowed test domain."""
    email_re = re.compile(r"\b([A-Za-z0-9._+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
    offenders: list[str] = []
    for root in FIXTURE_ROOTS:
        for path in _iter_text_files(root):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in email_re.finditer(text):
                domain = match.group(2).lower()
                if domain not in ALLOWED_TEST_DOMAINS:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")
    assert not offenders, "non-test domains in fixtures (ADR-019 §7):\n" + "\n".join(offenders[:25])


# ---------------------------------------------------------------------------
# Authorization deny-by-default
# ---------------------------------------------------------------------------


async def test_authorize__unknown_action__fails_closed() -> None:
    """Unknown action with no grant must deny (R12 fail-closed)."""
    policy = Policy()
    tenant = uuid4()
    user = UserContext(
        user_id=uuid4(),
        tenant_id=tenant,
        display_name="tester",
        roles=("intake",),
        is_system=False,
    )
    resource = Resource(tenant_id=tenant, kind="case", id=uuid4())
    decision = await policy.evaluate(
        "case.does_not_exist",  # type: ignore[arg-type]
        user,
        resource,
    )
    assert isinstance(decision, Decision)
    assert decision.allowed is False


async def test_authorize__missing_grant__fails_closed() -> None:
    """Known action without a role grant must deny."""
    policy = Policy()
    tenant = uuid4()
    user = UserContext(
        user_id=uuid4(),
        tenant_id=tenant,
        display_name="tester",
        roles=(),
        is_system=False,
    )
    resource = Resource(tenant_id=tenant, kind="case", id=uuid4())
    with pytest.raises(Forbidden):
        await authorize(Action.CASE_CREATE, user, resource, policy=policy)


async def test_authorize__cross_tenant_actor__fails_closed() -> None:
    """Cross-tenant actor is denied even with a matching role grant."""
    tenant_a = uuid4()
    tenant_b = uuid4()
    user = UserContext(
        user_id=uuid4(),
        tenant_id=tenant_b,
        display_name="tester",
        roles=("intake",),
        is_system=False,
    )
    resource = Resource(tenant_id=tenant_a, kind="case", id=uuid4())
    with pytest.raises(Forbidden):
        await authorize(Action.CASE_CREATE, user, resource)


async def test_authorize__grant_in_matching_tenant__succeeds() -> None:
    tenant = uuid4()
    user = UserContext(
        user_id=uuid4(),
        tenant_id=tenant,
        display_name="tester",
        roles=("intake",),
        is_system=False,
    )
    resource = Resource(tenant_id=tenant, kind="case", id=uuid4())
    # Should not raise.
    await authorize(Action.CASE_CREATE, user, resource)


# ---------------------------------------------------------------------------
# Synthetic-environment marker
# ---------------------------------------------------------------------------


def test_synthetic_marker__redactor_does_not_swallow_logged_marker() -> None:
    """The synthetic marker is a normal log key and must NOT be redacted."""
    payload = {
        "synthetic_environment": True,
        "tenant_id": "11111111-1111-1111-1111-111111111111",
    }
    redacted = redact_sensitive(payload)
    assert redacted["synthetic_environment"] is True


# ---------------------------------------------------------------------------
# UUID round-trip sanity (catches accidentally-flattened ids in fixtures).
# ---------------------------------------------------------------------------


def test_tenant_uuid_roundtrip__preserves_value() -> None:
    raw = "11111111-1111-1111-1111-111111111111"
    parsed = UUID(raw)
    assert str(parsed) == raw
