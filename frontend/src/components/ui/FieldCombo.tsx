/**
 * FieldCombo — the one control for "which field?".
 *
 * Six surfaces used to ask that question six ways: two native `<select>`s in
 * Investigate, one in Export, a Radix `Select` in Visualize and another in the
 * compare editor, and a bare inline `<select>` for the method knobs. None of
 * them let an analyst type, which is what you actually want when a timeline
 * carries three hundred `attr:*` tokens — and none of them could reach a field
 * the inventory had not reported yet.
 *
 * So: a text input that opens its full list on focus (browse, as the dropdowns
 * did), filters as you type against both the token and the label, and commits
 * whatever you type when nothing matches. The box shows the raw token, because
 * the token is what you would type and what every caller stores; the pretty
 * label and its hint live in the list rows.
 */
import { useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/cn";
import { useAnchoredDropdown } from "@/lib/useAnchoredDropdown";

export interface FieldComboOption {
  /** The token stored and emitted — `src_ip`, `attr:user_agent`. */
  value: string;
  /** What the row displays. */
  label: string;
  /** Dimmed trailing text on the row: cardinality, type, whatever the caller has. */
  hint?: string;
  /** Optional section header; rows keep the caller's order within a group. */
  group?: string;
}

interface Props {
  options: FieldComboOption[];
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  disabled?: boolean;
  /** Commit a token that is not in `options`. On by default. */
  allowFreeText?: boolean;
  /** Borderless, for a combo that sits inside a sentence (the method knobs). */
  variant?: "bordered" | "inline";
  /** `sm` matches the Investigate toolbars' compact row; `md` the sidebars'. */
  size?: "sm" | "md";
  className?: string;
  "aria-label"?: string;
  "data-testid"?: string;
}

export function FieldCombo({
  options,
  value,
  onChange,
  placeholder = "Choose a field…",
  disabled,
  allowFreeText = true,
  variant = "bordered",
  size = "md",
  className,
  "aria-label": ariaLabel,
  "data-testid": testId,
}: Props) {
  // `draft` is what the box shows while typing; `null` means "show the
  // committed value". Keeping them separate is what lets Escape revert without
  // the caller ever seeing the half-typed token.
  const [draft, setDraft] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [highlightIdx, setHighlightIdx] = useState(-1);

  const text = draft ?? value;

  const filtered = useMemo(() => {
    const q = (draft ?? "").trim().toLowerCase();
    if (!q) return options;
    return options.filter(
      (o) => o.value.toLowerCase().includes(q) || o.label.toLowerCase().includes(q),
    );
  }, [options, draft]);

  const { containerRef, listRef, pos } = useAnchoredDropdown({
    open,
    itemCount: filtered.length,
  });

  function commit(next: string) {
    onChange(next);
    setDraft(null);
    setOpen(false);
    setHighlightIdx(-1);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) setOpen(true);
      setHighlightIdx((i) => Math.min(i + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlightIdx((i) => Math.max(i - 1, -1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const hit = highlightIdx >= 0 ? filtered[highlightIdx] : undefined;
      if (hit) {
        commit(hit.value);
      } else if (draft !== null) {
        const typed = draft.trim();
        // One unambiguous match beats free text: typing a token in full and
        // pressing Enter should select it, not re-enter it as an unknown.
        const exact = options.find((o) => o.value.toLowerCase() === typed.toLowerCase());
        if (exact) commit(exact.value);
        else if (typed && allowFreeText) commit(typed);
        else if (!typed) commit("");
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      setDraft(null);
      setOpen(false);
      setHighlightIdx(-1);
    }
  }

  // Free entry is the point, but a typo commits just as quietly as a real
  // token and the chart or detector then comes back empty with nothing naming
  // the cause. Say it on the spot; never block on it — the inventory can lag a
  // source that was ingested a minute ago, and that field is still valid.
  const unknown = value !== "" && !options.some((o) => o.value === value);

  // Rows carry their group header inline rather than nesting lists, so the
  // flat index the keyboard walks stays the index of the row itself.
  const rows = useMemo(
    () =>
      filtered.map((o, i) => ({
        option: o,
        header: o.group && o.group !== filtered[i - 1]?.group ? o.group : null,
      })),
    [filtered],
  );

  return (
    <div ref={containerRef} className={cn("relative", className)}>
      <div className="relative">
        <input
          role="combobox"
          aria-expanded={open}
          aria-label={ariaLabel}
          data-testid={testId}
          disabled={disabled}
          placeholder={placeholder}
          value={text}
          onChange={(e) => {
            setDraft(e.target.value);
            setOpen(true);
            setHighlightIdx(-1);
          }}
          onKeyDown={handleKeyDown}
          onFocus={() => setOpen(true)}
          onBlur={() => {
            // Delay so a row's click lands first; a draft left unconfirmed is
            // dropped rather than committed, which is what clicking away means.
            setTimeout(() => {
              setDraft(null);
              setOpen(false);
              setHighlightIdx(-1);
            }, 150);
          }}
          className={cn(
            "w-full pr-6 text-[var(--color-fg-primary)] placeholder:text-[var(--color-fg-muted)] transition-base focus:outline-none disabled:opacity-40",
            variant === "bordered"
              ? cn(
                  "rounded border border-[var(--color-border-strong)] bg-[var(--color-bg-elevated)] focus:border-[var(--color-accent)]",
                  size === "sm" ? "h-7 px-2 text-xs" : "h-9 px-3 text-sm",
                )
              : "bg-transparent",
          )}
        />
        <button
          type="button"
          tabIndex={-1}
          // Out of the accessibility tree on purpose: the input is the control
          // (it carries `combobox` and `aria-expanded`, and opens on focus or
          // ArrowDown), so this is a redundant affordance for the mouse.
          aria-hidden="true"
          disabled={disabled}
          onMouseDown={(e) => {
            e.preventDefault();
            setOpen((o) => !o);
          }}
          className="absolute inset-y-0 right-0 flex items-center px-1.5 text-[var(--color-fg-muted)] disabled:opacity-40"
        >
          <ChevronDown size={14} />
        </button>
      </div>
      {open &&
        filtered.length > 0 &&
        createPortal(
          <ul
            ref={listRef}
            role="listbox"
            style={{ left: pos?.left ?? 0, top: pos?.top ?? 0, width: pos?.width ?? undefined }}
            className={cn(
              "fixed z-50 max-h-48 min-w-[10rem] overflow-y-auto rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] text-xs shadow-lg",
              pos ? "" : "invisible",
            )}
          >
            {rows.map(({ option: o, header }, i) => {
              return (
                <li key={o.value} role="presentation">
                  {header && (
                    <div className="px-2.5 pt-1.5 pb-0.5 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">
                      {header}
                    </div>
                  )}
                  <div
                    role="option"
                    aria-selected={o.value === value}
                    onMouseDown={(e) => {
                      e.preventDefault(); // keep focus on the input
                      commit(o.value);
                    }}
                    className={cn(
                      "flex cursor-pointer items-baseline justify-between gap-2 px-2.5 py-1.5 transition-colors",
                      i === highlightIdx
                        ? "bg-[var(--color-accent)] text-white"
                        : "text-[var(--color-fg-primary)] hover:bg-[var(--color-bg-hover)]",
                    )}
                  >
                    <span className="truncate">{o.label}</span>
                    {o.hint && (
                      <span
                        className={cn(
                          "shrink-0 text-xs",
                          i === highlightIdx ? "text-white/70" : "text-[var(--color-fg-muted)]",
                        )}
                      >
                        {o.hint}
                      </span>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>,
          document.body,
        )}
      {unknown && (
        <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
          Not in this timeline's reported fields — it may return nothing.
        </p>
      )}
    </div>
  );
}
