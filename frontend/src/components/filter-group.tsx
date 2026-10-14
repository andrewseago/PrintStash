import { ChevronDown } from "lucide-react";

/** One collapsible group of the Model filters panel. Every group uses it, so a
 *  facet list, the print history fields and the upload dates read as one family. */
export function FilterGroup({
  id,
  label,
  count = 0,
  open,
  onToggle,
  children,
}: {
  id: string;
  label: string;
  count?: number;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={onToggle}
        className="flex w-full items-center gap-1 rounded px-2 py-1.5 text-left text-sm font-medium text-foreground transition-[background-color,color,transform] duration-press hover:bg-muted active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ChevronDown
          className={`h-4 w-4 text-muted-foreground transition-transform duration-press ${open ? "" : "-rotate-90"}`}
        />
        <span className="flex-1">{label}</span>
        {count > 0 && (
          <span className="rounded-full bg-accent px-1.5 text-3xs text-accent-foreground">
            {count}
          </span>
        )}
      </button>
      {open && (
        <div id={id} className="ml-4 space-y-0.5 border-l border-border py-1 pl-3">
          {children}
        </div>
      )}
    </div>
  );
}
