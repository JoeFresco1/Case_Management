/** Route-level case intake surface (P01-US1 / T051).
 *
 * The published schema is supplied by the route/composition root. This page
 * deliberately does not invent a schema or call an unavailable configuration
 * endpoint; until that provider is wired, it reports the dependency honestly.
 */
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { useState } from "react";

import { ApiClientError, api } from "../api/client";
import type { components } from "../api/types.gen";
import type { CaseSummary } from "../api/types";
import { CreateCaseForm } from "./CreateCaseForm";
import styles from "./CaseWorkspace.module.css";

type CaseTypeSchema = components["schemas"]["CaseTypeSchemaRequest"];

export interface CreateCasePageProps {}

/** Render schema-driven intake, or an honest unavailable state before wiring. */
export function CreateCasePage(_props: CreateCasePageProps = {}) {
  const navigate = useNavigate();
  const query = useQuery({
    queryKey: ["case-types", "published"],
    queryFn: () => api.listCaseTypes(),
    staleTime: 5 * 60_000,
  });
  if (query.isLoading) return <p>Loading case types…</p>;
  if (query.error) {
    return (
      <p className={styles.error} role="alert">
        {query.error instanceof ApiClientError
          ? query.error.message
          : "Unable to load published case types."}
      </p>
    );
  }
  const types = query.data?.items ?? [];
  if (types.length === 0) {
    return (
      <section aria-labelledby="create-case-heading">
        <h1 id="create-case-heading">Create case</h1>
        <p className={styles.empty}>No published case types are available for intake.</p>
      </section>
    );
  }

  return <CreateCaseTypeSelection types={types} onCreated={(created) => void navigate(`/cases/${created.id}`)} />;
}

function CreateCaseTypeSelection({
  types,
  onCreated,
}: {
  types: components["schemas"]["PublishedCaseTypeResponse"][];
  onCreated: (created: CaseSummary) => void;
}) {
  const [selectedKey, setSelectedKey] = useState(types[0].key);
  const selected = types.find((item) => item.key === selectedKey) ?? types[0];

  return (
    <section aria-labelledby="create-case-heading">
      <label htmlFor="case-type">Case type</label>
      <select id="case-type" value={selected.key} onChange={(event) => setSelectedKey(event.target.value)}>
        {types.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}
      </select>
      <CreateCaseForm
        caseTypeKey={selected.key}
        publishedSchema={selected.schema as CaseTypeSchema}
        onCreated={onCreated}
      />
    </section>
  );
}

export default CreateCasePage;
