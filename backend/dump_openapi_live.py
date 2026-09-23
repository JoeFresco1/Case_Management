"""Dump live OpenAPI to frontend/openapi.live.json (sqlite profile, no Docker)."""

import json
import sys

sys.path.insert(0, ".")

from app.main import create_app

app = create_app()
spec = app.openapi()
with open("../frontend/openapi.live.json", "w", encoding="utf-8") as fh:
    json.dump(spec, fh, indent=2)
paths = spec.get("paths", {})
print("paths:", len(paths))
for key in (
    "AnnotationResponse",
    "CheckoutResponse",
    "RenditionResponse",
    "RevisionHistoryResponse",
    "BundleResponse",
    "MintLinkResponse",
    "RedeemLinkResponse",
    "XrefResponse",
    "SeparationPlanResponse",
    "PackageManifestResponse",
    "DedupResponse",
):
    print(key, key in spec.get("components", {}).get("schemas", {}))
