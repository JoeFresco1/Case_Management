import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { Icon } from "./Icon";
import styles from "./QuickOpen.module.css";

type Destination = { path: string; label: string };

/** Open a page or recent authorized case from anywhere using the keyboard. */
export function QuickOpen({ pages }: { pages: Destination[] }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [term, setTerm] = useState("");
  const navigate = useNavigate();
  const recent = useQuery({
    queryKey: ["cases", "list", { limit: 12, offset: 0 }],
    queryFn: () => api.listCases({ limit: 12, offset: 0 }),
    enabled: open,
  });
  function show() {
    if (dialog.current?.open) return;
    setTerm("");
    setOpen(true);
    dialog.current?.showModal();
    input.current?.focus();
  }
  useEffect(() => {
    function shortcut(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (dialog.current?.open) dialog.current.close();
        else show();
      }
    }
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, []);
  const needle = term.trim().toLowerCase();
  const cases = (recent.data?.items ?? []).filter((c) =>
    (c.title + " " + c.reference).toLowerCase().includes(needle),
  );
  const matches = pages.filter((p) => p.label.toLowerCase().includes(needle));
  function go(path: string) {
    dialog.current?.close();
    navigate(path);
  }
  return (
    <>
      <button
        className={styles.launch}
        onClick={show}
        aria-label="Quick open"
        aria-keyshortcuts="Control+k Meta+k"
      >
        <Icon name="search" />
        <span>Quick open</span>
        <kbd>Ctrl K</kbd>
      </button>
      <dialog
        ref={dialog}
        className={styles.dialog}
        aria-label="Quick open"
        onClose={() => setOpen(false)}
        onClick={(e) => {
          if (e.target === dialog.current) dialog.current.close();
        }}
        onKeyDown={(e) => {
          const buttons = Array.from(
            dialog.current?.querySelectorAll<HTMLButtonElement>(
              "[data-destination]",
            ) ?? [],
          );
          const index = buttons.indexOf(
            document.activeElement as HTMLButtonElement,
          );
          if (!buttons.length) return;
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            const next =
              e.key === "ArrowDown"
                ? index + 1
                : index <= 0
                  ? buttons.length - 1
                  : index - 1;
            buttons[next % buttons.length].focus();
          } else if (
            e.key === "Enter" &&
            document.activeElement === input.current
          ) {
            e.preventDefault();
            buttons[0].click();
          }
        }}
      >
        <div className={styles.search}>
          <Icon name="search" />
          <input
            ref={input}
            aria-label="Find a page or recent case"
            placeholder="Find a page or recent case…"
            value={term}
            onChange={(e) => setTerm(e.target.value)}
          />
          <button onClick={() => dialog.current?.close()}>Esc</button>
        </div>
        <div className={styles.results}>
          {cases.length > 0 && (
            <>
              <h2>Recent cases</h2>
              {cases.map((c) => (
                <button
                  data-destination
                  key={c.id}
                  onClick={() => go("/cases/" + c.id)}
                >
                  <Icon name="cases" />
                  <span>
                    {c.title}
                    <small>{c.reference}</small>
                  </span>
                </button>
              ))}
            </>
          )}
          {matches.length > 0 && (
            <>
              <h2>Go to</h2>
              {matches.map((p) => (
                <button
                  data-destination
                  key={p.path}
                  onClick={() => go(p.path)}
                >
                  <Icon name="menu" />
                  {p.label}
                </button>
              ))}
            </>
          )}
          {!matches.length && !cases.length && (
            <p>
              {recent.isLoading
                ? "Loading cases…"
                : "No matching pages or recent cases."}
            </p>
          )}
        </div>
      </dialog>
    </>
  );
}
