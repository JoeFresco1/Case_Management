/** Translate the documented aggregate query grammar to the existing report contract. */
import type { components } from "../api/types.gen";
type Spec = Pick<
  components["schemas"]["CustomReportDefinitionCreate"],
  "group_by" | "aggregations" | "filters"
>;
export const reportFields = ["status", "priority", "workflow_state"] as const;
const field = "(status|priority|workflow_state)";

/** Reject any syntax outside the supported count/group/equality grammar. */
export function compileReportQuery(sql: string): Spec {
  const match = new RegExp(
    "^\\s*SELECT\\s+" +
      field +
      "\\s*,\\s*COUNT\\s*\\(\\s*\\*\\s*\\)\\s+FROM\\s+cases(?:\\s+WHERE\\s+" +
      field +
      "\\s*=\\s*'([^'\\r\\n]*)')?\\s+GROUP\\s+BY\\s+" +
      field +
      "\\s*;?\\s*$",
    "i",
  ).exec(sql);
  if (!match || match[1].toLowerCase() !== match[4].toLowerCase())
    throw new Error(
      "Use SELECT field, COUNT(*) FROM cases [WHERE field = 'value'] GROUP BY field. Supported fields: status, priority, workflow_state.",
    );
  return {
    group_by: ["case." + match[1].toLowerCase()],
    aggregations: ["count"],
    filters: match[2]
      ? [{ field: "case." + match[2].toLowerCase(), op: "eq", value: match[3] }]
      : [],
  };
}
