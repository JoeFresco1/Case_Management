import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { CaseSummary } from "../api/types";
import {
  listPublishedForms,
  listPublishedBindings,
} from "../api/client/formCatalog";
import { FormRenderer } from "./FormRenderer";
import styles from "./CaseForms.module.css";

/** Discover published forms bound to this case type, keeping authorization on the server. */
export function CaseForms({ caseSummary }: { caseSummary: CaseSummary }) {
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState("");
  const catalog = useQuery({
    queryKey: ["case-forms", caseSummary.case_type_version_id, offset],
    queryFn: async () => {
      const page = await listPublishedForms(offset);
      const matching = await Promise.all(
        page.items.map(async (form) => {
          let pageOffset = 0;
          let more = true;
          while (more) {
            const bindings = await listPublishedBindings(form.key, pageOffset);
            if (
              bindings.items.some(
                (b) =>
                  b.case_type_version_id === caseSummary.case_type_version_id,
              )
            )
              return form;
            more = Boolean(bindings.next_cursor);
            pageOffset += bindings.limit;
          }
        }),
      );
      return {
        items: matching.filter((form) => form != null),
        next: page.next_cursor,
      };
    },
    retry: false,
  });
  return (
    <section className={styles.forms}>
      <h2>Case forms</h2>
      <p>Published forms for this case type.</p>
      {catalog.isLoading && <p>Loading forms…</p>}
      {catalog.isError && (
        <p role="alert">Forms could not be loaded for this workspace.</p>
      )}
      {catalog.data && !catalog.data.items.length && (
        <p>No forms on this page are published for this case type.</p>
      )}
      {catalog.data?.items.map((form) => (
        <button
          type="button"
          key={form.id}
          aria-pressed={selected === form.key}
          onClick={() => setSelected(form.key)}
        >
          {form.key.split("_").join(" ")}{" "}
          <small>Version {form.version_number}</small>
        </button>
      ))}
      {(offset > 0 || catalog.data?.next) && (
        <nav aria-label="Form pages">
          <button
            disabled={!offset}
            onClick={() => {
              setSelected("");
              setOffset(Math.max(0, offset - 50));
            }}
          >
            Previous forms
          </button>
          <button
            disabled={!catalog.data?.next}
            onClick={() => {
              setSelected("");
              setOffset(offset + 50);
            }}
          >
            Next forms
          </button>
        </nav>
      )}
      {selected && (
        <div className={styles.runtime}>
          <FormRenderer
            key={caseSummary.id + selected}
            caseId={caseSummary.id}
            formKey={selected}
            initialData={caseSummary.data}
          />
        </div>
      )}
    </section>
  );
}
