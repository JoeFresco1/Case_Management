import { BundleSharing } from "./BundleSharing";

/**
 * Standalone bundle-sharing route (T5-US3, C-8).
 *
 * Permissioned bundles + share-by-link console: create/open/grant,
 * member edits, revoke, mint/redeem/revoke links. All reads stay
 * metadata-only; denials never confirm bundle contents.
 */
export function BundleSharingPage() {
  return (
    <article>
      <h1>Bundles</h1>
      <BundleSharing />
    </article>
  );
}
