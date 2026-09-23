"""Stable viewer event-type literals.

Subscribers (outbox consumers, the timeline projection, Azure-search
convergence hooks) depend on these exact string values, so they live
in one place shared by every viewer use case. Renaming a literal here
is a contract change, not a refactor.
"""

from __future__ import annotations

ANNOTATION_CREATED = "AnnotationCreated"
ANNOTATION_UPDATED = "AnnotationUpdated"
ANNOTATION_DELETED = "AnnotationDeleted"
DOCUMENT_CHECKED_OUT = "DocumentCheckedOut"
DOCUMENT_CHECKOUT_RENEWED = "DocumentCheckoutRenewed"
DOCUMENT_CHECKED_IN = "DocumentCheckedIn"
RENDITION_BURNED = "RenditionBurned"
DOCUMENT_REINDEXED = "DocumentReindexed"

__all__ = [
    "ANNOTATION_CREATED",
    "ANNOTATION_DELETED",
    "ANNOTATION_UPDATED",
    "DOCUMENT_CHECKED_IN",
    "DOCUMENT_CHECKED_OUT",
    "DOCUMENT_CHECKOUT_RENEWED",
    "DOCUMENT_REINDEXED",
    "RENDITION_BURNED",
]
