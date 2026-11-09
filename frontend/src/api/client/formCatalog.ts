/** Published form discovery using the generated API contracts. */
import type { components } from "../types.gen";
import { request } from "./http";

export function listPublishedForms(offset = 0) {
  return request<components["schemas"]["Page_FormListItem_"]>(
    "/forms?limit=50&offset=" + offset,
  );
}
export function listPublishedBindings(key: string, offset = 0) {
  return request<components["schemas"]["Page_FormBindingListItem_"]>(
    "/forms/" + encodeURIComponent(key) + "/bindings?limit=50&offset=" + offset,
  );
}
