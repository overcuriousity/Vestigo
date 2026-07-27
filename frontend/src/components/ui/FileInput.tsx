/**
 * File-picking primitives.
 *
 * Four sites used to hand-roll `<input type="file">` and had drifted: only
 * three cleared the input's value after a pick (without it, re-picking the
 * same file after a failed upload fires no `change` event and looks like a
 * dead button), only one filtered dropped files, and the hidden inputs sat in
 * the tab order beside the visible control that opens them.
 *
 * Three exports, one per shape actually in use: the bare input, a drop zone,
 * and a button that opens the picker. No `cva` — none of these has a variant
 * axis (the drop zone's `dragging` is state, not a variant).
 */
import { forwardRef, useRef, useState, type ComponentProps, type ReactNode } from "react";
import { FileText } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { fmtBytes } from "@/lib/format";
import { cn } from "@/lib/cn";

export interface FileInputProps
  extends Omit<
    React.InputHTMLAttributes<HTMLInputElement>,
    "type" | "onChange" | "value" | "defaultValue"
  > {
  /** Comma-separated extension/MIME list, e.g. ".yml,.yaml". */
  accept?: string;
  multiple?: boolean;
  /** Picked files, always an array (empty when the dialog was dismissed). */
  onFiles: (files: File[]) => void;
  /**
   * Visually hidden but still a real input the caller can `.click()` — for
   * button- and drop-zone-triggered pickers. Deliberately not the native
   * `hidden` attribute (it would collide with the prop spread) and not
   * `display: none` (which makes `.click()` unreliable in some browsers).
   *
   * Callers must keep it out of the tab order (`tabIndex={-1}`): the visible
   * control they wrap it in is the focusable one, and two tab stops for one
   * control — with an interactive element nested inside a `role="button"` —
   * is worse for keyboard users than none.
   */
  srOnly?: boolean;
  /**
   * Keep the native value after a pick. Default false: the value is cleared so
   * re-picking the same file fires `change` again.
   */
  keepValue?: boolean;
}

export const FileInput = forwardRef<HTMLInputElement, FileInputProps>(
  ({ onFiles, srOnly, keepValue, className, ...props }, ref) => (
    <input
      ref={ref}
      type="file"
      className={cn(
        srOnly
          ? "absolute h-0 w-0 overflow-hidden opacity-0"
          : "block w-full text-sm text-[var(--color-fg-primary)] disabled:opacity-40",
        className,
      )}
      onChange={(e) => {
        onFiles(Array.from(e.target.files ?? []));
        if (!keepValue) e.target.value = "";
      }}
      {...props}
    />
  ),
);
FileInput.displayName = "FileInput";

/**
 * True when `file` matches one entry of a comma-separated `accept` list —
 * `.ext`, `type/subtype`, or `type/*`, the three forms the HTML attribute
 * takes. An unrecognized entry matches nothing rather than everything: this
 * gate exists to make drag-drop agree with the picker, and a rule nobody can
 * satisfy is safer than one everybody satisfies.
 */
function matchesAccept(file: File, accept: string | undefined): boolean {
  if (!accept) return true;
  const name = file.name.toLowerCase();
  const type = file.type.toLowerCase();
  return accept
    .split(",")
    .map((a) => a.trim().toLowerCase())
    .filter(Boolean)
    .some((a) => {
      if (a.startsWith(".")) return name.endsWith(a);
      if (a.endsWith("/*")) return !!type && type.startsWith(a.slice(0, -1));
      // A MIME rule can only be checked when the browser guessed a type; it
      // routinely doesn't for forensic formats (.jsonl, .parquet, .vestigo).
      return !!type && type === a;
    });
}

export interface FileDropZoneProps {
  accept?: string;
  multiple?: boolean;
  /** Current selection, rendered as name + size when present. */
  files?: File[] | File | null;
  onFiles: (files: File[]) => void;
  icon?: ReactNode;
  prompt?: ReactNode;
  hint?: ReactNode;
  disabled?: boolean;
  className?: string;
}

export function FileDropZone({
  accept,
  multiple,
  files,
  onFiles,
  icon,
  prompt = "Drop a file here or click to browse",
  hint,
  disabled,
  className,
  ...rest
}: FileDropZoneProps & Omit<ComponentProps<"div">, keyof FileDropZoneProps>) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const selected = files == null ? [] : Array.isArray(files) ? files : [files];

  const open = () => {
    if (!disabled) inputRef.current?.click();
  };

  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled || undefined}
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        if (disabled) return;
        // The browser only enforces `accept` in the picker, so filter here too
        // — otherwise drag-drop silently accepts what clicking cannot.
        const dropped = Array.from(e.dataTransfer.files).filter((f) =>
          matchesAccept(f, accept),
        );
        if (dropped.length > 0) onFiles(multiple ? dropped : dropped.slice(0, 1));
      }}
      onClick={open}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      }}
      className={cn(
        "relative flex flex-col items-center gap-2 rounded-lg border-2 border-dashed px-6 py-8 text-center transition-base",
        disabled
          ? "cursor-not-allowed border-[var(--color-border)] opacity-50"
          : "cursor-pointer",
        !disabled && dragging
          ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)]"
          : !disabled &&
              "border-[var(--color-border-strong)] bg-[var(--color-bg-base)] hover:border-[var(--color-accent)] hover:bg-[var(--color-accent-dim)]",
        className,
      )}
      {...rest}
    >
      {icon ?? <FileText size={28} className="text-[var(--color-fg-muted)] opacity-60" />}
      {selected.length > 0 ? (
        <div>
          {selected.map((f) => (
            <div key={`${f.name}-${f.size}`}>
              <p className="text-sm font-medium text-[var(--color-fg-primary)]">{f.name}</p>
              <p className="text-xs text-[var(--color-fg-muted)]">{fmtBytes(f.size)}</p>
            </div>
          ))}
        </div>
      ) : (
        <>
          <p className="text-sm text-[var(--color-fg-secondary)]">{prompt}</p>
          {hint && <p className="text-xs text-[var(--color-fg-muted)]">{hint}</p>}
        </>
      )}
      <FileInput
        ref={inputRef}
        srOnly
        // The zone itself is the tab stop and handles Enter/Space.
        tabIndex={-1}
        accept={accept}
        multiple={multiple}
        disabled={disabled}
        // The programmatic `.click()` dispatches an event that bubbles back to
        // this zone's own onClick, which would open the picker a second time.
        onClick={(e) => e.stopPropagation()}
        onFiles={onFiles}
      />
    </div>
  );
}

export interface FileInputButtonProps {
  accept?: string;
  multiple?: boolean;
  onFiles: (files: File[]) => void;
  /** Idle label. */
  children: ReactNode;
  pending?: boolean;
  pendingLabel?: ReactNode;
  icon?: ReactNode;
  variant?: ComponentProps<typeof Button>["variant"];
  size?: ComponentProps<typeof Button>["size"];
  disabled?: boolean;
  className?: string;
}

export function FileInputButton({
  accept,
  multiple,
  onFiles,
  children,
  pending,
  pendingLabel = "Uploading…",
  icon,
  variant = "outline",
  size = "sm",
  disabled,
  className,
}: FileInputButtonProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const blocked = disabled || pending;

  return (
    <span className={cn("relative inline-flex", className)}>
      <Button
        variant={variant}
        size={size}
        disabled={blocked}
        onClick={() => inputRef.current?.click()}
      >
        {icon}
        {pending ? pendingLabel : children}
      </Button>
      <FileInput
        ref={inputRef}
        srOnly
        // The Button is the tab stop; this input is reached through it.
        tabIndex={-1}
        accept={accept}
        multiple={multiple}
        disabled={blocked}
        onFiles={onFiles}
      />
    </span>
  );
}
