/** Report authoring uses generated contracts and the shared authorized client. */
import type { components } from "../types.gen";
import { request } from "./http";
export function defineReport(
  body: components["schemas"]["CustomReportDefinitionCreate"],
  key: string,
) {
  return request<components["schemas"]["CustomReportDefinitionResponse"]>(
    "/reports/custom-definitions",
    {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify(body),
    },
  );
}
