/** Line icons shared by the shell's navigation and action controls. */
const paths = {
  home: "m3 10 9-7 9 7v10h-6v-6H9v6H3z",
  cases: "M3 7V5h6l2 2h10v13H3z",
  tasks: "M8 5H5v16h14V5h-3 M9 3h6v4H9z m-1 10 3 3 5-6",
  queues: "M4 5h16v4H4z M4 13h16 M4 18h12",
  search: "M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0z m-2 5 6 6",
  bundles: "m3 8 9-5 9 5-9 5z M3 12l9 5 9-5 M3 16l9 5 9-5",
  dashboard: "M4 20h16 M6 16v-5 M12 16V4 M18 16V8",
  audit: "M4 10a8 8 0 1 1 1 7 M4 4v6h6 M12 7v5l3 2",
  settings:
    "M4 6h6m4 0h6 M4 12h11m4 0h1 M4 18h2m4 0h10 M10 4v4 M15 10v4 M6 16v4",
  menu: "M3 6h18M3 12h18M3 18h18",
  plus: "M12 5v14M5 12h14",
  chevron: "m8 10 4 4 4-4",
} as const;

export type IconName = keyof typeof paths;

/** Render a decorative icon paired with a visible or accessible text label. */
export function Icon({ name }: { name: IconName }) {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}
