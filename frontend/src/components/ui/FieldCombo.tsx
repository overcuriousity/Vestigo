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
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
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
  // The blur close is deferred so a row's click lands first, which means it can
  // still be in flight when the analyst comes back — refocusing and typing
  // inside that window used to have the stale timer wipe the fresh draft.
  const blurTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const listId = useId();

  const text = draft ?? value;

  const cancelBlurClose = useCallback(() => {
    if (blurTimer.current !== null) {
      clearTimeout(blurTimer.current);
      blurTimer.current = null;
    }
  }, []);
  useEffect(() => cancelBlurClose, [cancelBlurClose]);

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

  const close = useCallback(() => {
    cancelBlurClose();
    setDraft(null);
    setOpen(false);
    setHighlightIdx(-1);
  }, [cancelBlurClose]);

  function commit(next: string) {
    onChange(next);
    close();
  }

  // Escape reverts the draft and must not reach anything else. Both surfaces
  // this lives in also close on Escape — `InvestigateSheet` from a `window`
  // listener, a Radix dialog from a *capture*-phase `document` one that runs
  // before any React handler — so an `onKeyDown` here loses the race: reverting
  // a half-typed token threw away every knob value in the sheet, or shut the
  // export dialog outright. A capture listener on `window` is the one position
  // ahead of both.
  const escapeArmed = open || draft !== null;
  useEffect(() => {
    if (!escapeArmed) return;
    const container = containerRef.current;
    function onKey(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      // Only the key the analyst pressed *in this box*: an open list whose
      // input has already lost focus must not swallow the sheet's own Escape.
      const target = e.target;
      if (!(target instanceof Node) || !container?.contains(target)) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      close();
    }
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [escapeArmed, close, containerRef]);

  // The list is `max-h-48` and these run to hundreds of tokens, which is the
  // reason this control exists — an arrow key that moves an off-screen
  // highlight would have Enter commit something the analyst cannot see.
  useEffect(() => {
    if (!open || highlightIdx < 0) return;
    const row = listRef.current?.querySelector<HTMLElement>(`[data-idx="${highlightIdx}"]`);
    // jsdom has no layout, so it ships no `scrollIntoView`.
    row?.scrollIntoView?.({ block: "nearest" });
  }, [open, highlightIdx, listRef]);

  // `""` is a real choice only where the caller offers one — it is the method's
  // own default in `MethodFieldSelect`. Everywhere else the list cannot express
  // it and the query cannot use it, so an emptied box keeps its draft rather
  // than issuing a fieldless request that comes back wrong with nothing saying
  // why.
  const hasEmptyOption = useMemo(() => options.some((o) => o.value === ""), [options]);

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
        // The filter matches the label as well as the token, so the label has
        // to be honoured here too — otherwise typing what a row *displays*
        // narrows the list to that one row and Enter commits the display text
        // as an unknown token: `series_field = "Display name"`, `user_agent`
        // where the option is `attr:user_agent`, the literal `No grouping`
        // where the row means "clear this". Ambiguous labels fall through to
        // free text rather than guessing which row was meant.
        const lowered = typed.toLowerCase();
        const exact = options.find((o) => o.value.toLowerCase() === lowered);
        const byLabel = typed ? options.filter((o) => o.label.toLowerCase() === lowered) : [];
        if (exact) commit(exact.value);
        else if (byLabel.length === 1) commit(byLabel[0].value);
        else if (typed && allowFreeText) commit(typed);
        else if (!typed && hasEmptyOption) commit("");
      }
    }
  }

  // Free entry is the point, but a typo commits just as quietly as a real
  // token and the chart or detector then comes back empty with nothing naming
  // the cause. Say it on the spot; never block on it — the inventory can lag a
  // source that was ingested a minute ago, and that field is still valid.
  // An empty list is not evidence of anything: until the inventory query lands
  // every value is "unknown", and a field named in the URL is not suspect
  // because a fetch is still in flight.
  const unknown = options.length > 0 && value !== "" && !options.some((o) => o.value === value);

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

  const listOpen = open && filtered.length > 0;

  return (
    <div ref={containerRef} className={cn("relative", className)}>
      <div className="relative">
        <input
          role="combobox"
          aria-expanded={open}
          aria-autocomplete="list"
          aria-controls={listOpen ? listId : undefined}
          // Without this a screen reader hears nothing as ↓/↑ walk the list —
          // the Radix `Select` this replaces announced its active item.
          aria-activedescendant={
            listOpen && highlightIdx >= 0 ? `${listId}-opt-${highlightIdx}` : undefined
          }
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
          onFocus={() => {
            cancelBlurClose();
            setOpen(true);
          }}
          onBlur={() => {
            // Delay so a row's click lands first; a draft left unconfirmed is
            // dropped rather than committed, which is what clicking away means.
            cancelBlurClose();
            blurTimer.current = setTimeout(() => {
              blurTimer.current = null;
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
      {listOpen &&
        createPortal(
          <ul
            ref={listRef}
            id={listId}
            role="listbox"
            style={{ left: pos?.left ?? 0, top: pos?.top ?? 0, width: pos?.width ?? undefined }}
            // `pointer-events-auto` and the z-index above a dialog's `z-50` are
            // both for `ExportDialog`: a modal Radix layer sets
            // `body { pointer-events: none }` and re-enables it on its own node
            // only, so a list portaled to `document.body` was mouse-dead —
            // every row click swallowed, keyboard the sole way through.
            className={cn(
              "pointer-events-auto fixed z-[60] max-h-48 min-w-[10rem] overflow-y-auto rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] text-xs shadow-lg",
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
                    id={`${listId}-opt-${i}`}
                    data-idx={i}
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
