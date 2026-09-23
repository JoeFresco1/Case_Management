"""Document-operations use cases: x-refs, separation plans, packages, dedup, CSV, bundles (T5).

# Why a package

Formerly a single 1359-line module exceeding the R2 1000-line hard
cap. Split by responsibility into :mod:`xrefs`, :mod:`separation`,
:mod:`packages`, and :mod:`bundles` over a shared :mod:`_common`
module. This ``__init__`` is a pure re-export shim so existing
imports (``from app.application.documents.doc_ops import X``) keep
resolving without touching routers or tests.

# Storage

X-refs, bundles, grants, and links live in the in-memory stores
below (``InMemoryXrefStore`` / ``InMemoryBundleStore`` /
``InMemoryLinkStore``). They are process-local singletons held on
``app.state`` — the same pattern as the H0 legal-hold in-memory
repository — so the HTTP surface is wired end-to-end against
synthetic tenants without a migration. A SQL adapter can replace
these Protocols without touching the use cases.

# INTEGRATION_SEAM: none (local deterministic logic + in-memory rows)

# Failure semantics

- Auth / not-found / validation errors raise before any write.
- Every mutation appends its audit row + outbox row in the same
  transaction as the domain row (FR-L02); the router commits once.
"""

from __future__ import annotations

from ._common import (
    CSV_EXPORTED,
    DOC_BUNDLE_CREATED,
    DOC_BUNDLE_MEMBERS_CHANGED,
    DOC_BUNDLE_REVOKED,
    DOC_PACKAGE_BUILT,
    DOC_XREF_CREATED,
    SEPARATION_PLANNED,
    SHARE_LINK_MINTED,
    SHARE_LINK_REDEEMED,
    SHARE_LINK_REVOKED,
    SUBJECT_TYPE_BUNDLE,
    SUBJECT_TYPE_DOCUMENT,
)
from .bundles import (
    BundleStore,
    CreateBundleCommand,
    InMemoryBundleStore,
    InMemoryLinkStore,
    LinkStore,
    MintedLink,
    MintLinkCommand,
    RedeemedLink,
    change_bundle_members,
    create_bundle,
    grant_bundle_access,
    mint_share_link,
    redeem_share_link,
    revoke_bundle,
    revoke_share_link,
)
from .packages import (
    BuildPackageCommand,
    ExportCsvCommand,
    build_package,
    export_csv,
    find_duplicates,
)
from .separation import (
    PlanJoinMergeCommand,
    PlanSplitCommand,
    plan_separation_join_merge,
    plan_separation_split,
)
from .xrefs import (
    CreateXrefCommand,
    InMemoryXrefStore,
    XrefStore,
    create_xref,
    list_xrefs,
)

__all__ = [
    "CSV_EXPORTED",
    "DOC_BUNDLE_CREATED",
    "DOC_BUNDLE_MEMBERS_CHANGED",
    "DOC_BUNDLE_REVOKED",
    "DOC_PACKAGE_BUILT",
    "DOC_XREF_CREATED",
    "SEPARATION_PLANNED",
    "SHARE_LINK_MINTED",
    "SHARE_LINK_REDEEMED",
    "SHARE_LINK_REVOKED",
    "SUBJECT_TYPE_BUNDLE",
    "SUBJECT_TYPE_DOCUMENT",
    "BuildPackageCommand",
    "BundleStore",
    "CreateBundleCommand",
    "CreateXrefCommand",
    "ExportCsvCommand",
    "InMemoryBundleStore",
    "InMemoryLinkStore",
    "InMemoryXrefStore",
    "LinkStore",
    "MintLinkCommand",
    "MintedLink",
    "PlanJoinMergeCommand",
    "PlanSplitCommand",
    "RedeemedLink",
    "XrefStore",
    "build_package",
    "change_bundle_members",
    "create_bundle",
    "create_xref",
    "export_csv",
    "find_duplicates",
    "grant_bundle_access",
    "list_xrefs",
    "mint_share_link",
    "plan_separation_join_merge",
    "plan_separation_split",
    "redeem_share_link",
    "revoke_bundle",
    "revoke_share_link",
]
