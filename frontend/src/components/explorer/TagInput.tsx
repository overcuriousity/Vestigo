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
import { useState, useRef, useEffect } from "react";
import { Input } from "@/components/ui/Input";
import { cn } from "@/lib/cn";

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
}: Props) {
  const [highlightIdx, setHighlightIdx] = useState(-1);
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Filter suggestions by substring match
  const filtered = value.trim()
    ? suggestions.filter((s) =>
        s.toLowerCase().includes(value.toLowerCase()),
      )
    : [];

  // Reset highlight when filtered list changes
  useEffect(() => {
    setHighlightIdx(-1);
  }, [value]);

  // Open dropdown whenever filtered list is non-empty
  useEffect(() => {
    setOpen(filtered.length > 0);
  }, [filtered.length]);

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
          // Small delay so click on suggestion is registered first
          setTimeout(() => setOpen(false), 150);
        }}
        onFocus={() => {
          if (filtered.length > 0) setOpen(true);
        }}
      />
      {open && (
        <ul className="absolute z-50 mt-0.5 w-full min-w-[10rem] max-h-48 overflow-y-auto rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] shadow-lg text-xs">
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
        </ul>
      )}
    </div>
  );
}
