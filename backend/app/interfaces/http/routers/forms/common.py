"""Shared router and published-form response converters.

# Why this module (R2 structural split)

The former single-file ``forms.py`` module (720 lines) passed the R2
600-line soft limit. This module owns the pieces every route group in
the package shares:

- :data:`router` — ``APIRouter(prefix="/forms", tags=["forms"])`` that
  the submission (:mod:`submission`), published-read (:mod:`versions`),
  multi-step session (:mod:`sessions`), and transition-check
  (:mod:`transitions`) route modules register onto. ``app.main`` imports
  it from the package ``__init__``, so the consumer contract is
  unchanged.
- the domain-to-schema converters (:func:`_form_version_to_response`,
  :func:`_form_binding_to_response`) the published-read routes use.

Registration order across the sibling route modules is irrelevant: no
two form paths overlap at the same segment depth and method, so
Starlette's first-match rule resolves identically regardless of import
order.

# INTEGRATION_SEAM: HTTP entry

The routes delegate to the ``app.application.forms.*`` use cases;
Azure / corporate wiring plugs in behind those application ports, never
here.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.domain.form import FormBinding, FormVersion
from app.interfaces.http.schemas.forms import FormBindingResponse, FormVersionResponse

router = APIRouter(prefix="/forms", tags=["forms"])


def _form_version_to_response(fv: FormVersion) -> FormVersionResponse:
    """Map a domain ``FormVersion`` onto its wire schema."""
    return FormVersionResponse(
        id=fv.id,
        tenant_id=fv.tenant_id,
        key=fv.key,
        version_number=fv.version_number,
        definition=fv.definition,
        status=fv.status.value,
        published_at=fv.published_at.isoformat() if fv.published_at else None,
        published_by=fv.published_by,
        created_at=fv.created_at.isoformat() if fv.created_at else "",
    )


def _form_binding_to_response(fb: FormBinding) -> FormBindingResponse:
    """Map a domain ``FormBinding`` onto its wire schema."""
    return FormBindingResponse(
        id=fb.id,
        tenant_id=fb.tenant_id,
        case_type_version_id=fb.case_type_version_id,
        form_version_id=fb.form_version_id,
        published_at=fb.published_at.isoformat() if fb.published_at else None,
        published_by=fb.published_by,
        created_at=fb.created_at.isoformat() if fb.created_at else "",
    )
