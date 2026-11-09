import { describe, expect, it } from "vitest";
import { compileReportQuery } from "./reportQuery";
describe("Report query grammar", () => {
  it("compiles supported aggregate and equality filter", () => {
    expect(
      compileReportQuery(
        "SELECT priority, COUNT(*) FROM cases WHERE status = 'open' GROUP BY priority;",
      ),
    ).toEqual({
      group_by: ["case.priority"],
      aggregations: ["count"],
      filters: [{ field: "case.status", op: "eq", value: "open" }],
    });
  });
  it.each([
    "SELECT * FROM cases",
    "DELETE FROM cases",
    "SELECT priority, COUNT(*) FROM cases GROUP BY status",
    "SELECT status, COUNT(*) FROM cases WHERE status = 'open'; DROP TABLE cases; GROUP BY status",
  ])("rejects unsupported query %s", (query) => {
    expect(() => compileReportQuery(query)).toThrow();
  });
});
