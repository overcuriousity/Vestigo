/**
 * TagInput — tag-mode text field with autocomplete suggestions.
 *
 * Renders a plain Input, and when the field has a value shows a dropdown of
 * filtered existing annotation-tag suggestions beneath it. Free text is always
 * allowed (tags are open-vocabulary).
 *
 * Keyboard: ↓/↑ to highlight, Enter to accept highlight or submit free text,
 * Escape to close dropdown / cancel.
 */
import { useState, useRef, useEffect, useLayoutEffect } from "react";
import { createPortal } from "react-dom";
import { Input } from "@/components/ui/Input";
import { cn } from "@/lib/cn";

/** Max dropdown height in px — must match the `max-h-48` (12rem) below. */
const DROPDOWN_MAX_HEIGHT = 192;
const DROPDOWN_GAP = 2;

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSubmit: (v: string) => void;
  onCancel: () => void;
  suggestions: string[];
  isPending?: boolean;
  placeholder?: string;
  className?: string;
  autoFocus?: boolean;
  /** Render the suggestion dropdown above the input instead of below. */
  dropUp?: boolean;
  /** Show the full suggestion list on focus even before any text is typed. */
  openOnFocus?: boolean;
}

export function TagInput({
  value,
  onChange,
  onSubmit,
  onCancel,
  suggestions,
  isPending,
  placeholder = "tag label…",
  className,
  autoFocus,
  dropUp = false,
  openOnFocus = false,
}: Props) {
  const [highlightIdx, setHighlightIdx] = useState(-1);
  const [open, setOpen] = useState(false);
  const [focused, setFocused] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  // Fixed viewport coordinates for the portaled dropdown, so it escapes any
  // scrolling/overflow-clipping ancestor and can flip above/below to fit.
  const [pos, setPos] = useState<{ left: number; top: number; width: number } | null>(null);

  // Filter suggestions by substring match; with openOnFocus, an empty input
  // offers the full list while focused (e.g. browse available field names).
  const filtered = value.trim()
    ? suggestions.filter((s) =>
        s.toLowerCase().includes(value.toLowerCase()),
      )
    : openOnFocus && focused
      ? suggestions
      : [];

  // Reset highlight when filtered list changes
  useEffect(() => {
    setHighlightIdx(-1);
  }, [value]);

  // Open dropdown whenever filtered list is non-empty
  useEffect(() => {
    setOpen(filtered.length > 0);
  }, [filtered.length]);

  // Anchor the portaled dropdown to the input, flipping above when there is
  // no room below (honouring `dropUp` only as the initial preference). Runs
  // after layout and on any scroll/resize while open.
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const anchor = containerRef.current?.getBoundingClientRect();
      if (!anchor) return;
      const listHeight = Math.min(listRef.current?.scrollHeight ?? 0, DROPDOWN_MAX_HEIGHT);
      const roomBelow = window.innerHeight - anchor.bottom;
      const roomAbove = anchor.top;
      const placeUp = dropUp
        ? roomAbove >= listHeight || roomAbove > roomBelow
        : roomBelow < listHeight && roomAbove > roomBelow;
      const top = placeUp
        ? anchor.top - DROPDOWN_GAP - listHeight
        : anchor.bottom + DROPDOWN_GAP;
      setPos({ left: anchor.left, top, width: anchor.width });
    };
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, dropUp, filtered.length]);

  function accept(tag: string) {
    onSubmit(tag);
    setOpen(false);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlightIdx((i) => Math.min(i + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlightIdx((i) => Math.max(i - 1, -1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (highlightIdx >= 0 && filtered[highlightIdx]) {
        accept(filtered[highlightIdx]);
      } else if (value.trim()) {
        accept(value.trim());
      }
    } else if (e.key === "Escape") {
      if (open) {
        setOpen(false);
      } else {
        onCancel();
      }
    }
  }

  return (
    <div ref={containerRef} className={cn("relative", className)}>
      <Input
        autoFocus={autoFocus}
        placeholder={placeholder}
        value={value}
        disabled={isPending}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        onBlur={() => {
          setFocused(false);
          // Small delay so click on suggestion is registered first
          setTimeout(() => setOpen(false), 150);
        }}
        onFocus={() => {
          setFocused(true);
          if (filtered.length > 0 || (openOnFocus && suggestions.length > 0)) setOpen(true);
        }}
      />
      {open &&
        createPortal(
          <ul
            ref={listRef}
            style={{
              left: pos?.left ?? 0,
              top: pos?.top ?? 0,
              width: pos?.width ?? undefined,
            }}
            className={cn(
              "fixed z-50 min-w-[10rem] max-h-48 overflow-y-auto rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] shadow-lg text-xs",
              pos ? "" : "invisible",
            )}
          >
            {filtered.map((tag, i) => (
              <li
                key={tag}
                onMouseDown={(e) => {
                  e.preventDefault(); // keep focus on Input
                  accept(tag);
                }}
                className={cn(
                  "cursor-pointer px-2.5 py-1.5 truncate transition-colors",
                  i === highlightIdx
                    ? "bg-[var(--color-accent)] text-white"
                    : "text-[var(--color-fg-primary)] hover:bg-[var(--color-bg-hover)]",
                )}
              >
                {tag}
              </li>
            ))}
          </ul>,
          document.body,
        )}
    </div>
  );
}
