/**
 * The router's last net — what renders when a failure escapes even the
 * `AppShell` boundary (a throw in the shell itself, or in routing). React
 * Router's built-in fallback is a developer stack trace on a white page; this
 * at least tells an analyst what happened and how to get back.
 */
import { Link, useRouteError } from "react-router-dom";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/Button";

export function RouteErrorPage() {
  const error = useRouteError();
  const message =
    error instanceof Error
      ? error.message
      : typeof error === "string"
        ? error
        : "Unknown error";

  return (
    <div className="flex h-svh flex-col items-center justify-center gap-4 bg-[var(--color-bg-base)] p-6 text-center">
      <AlertTriangle size={48} className="text-[var(--color-danger)] opacity-60" />
      <h2 className="text-lg font-semibold text-[var(--color-fg-primary)]">
        Something went wrong
      </h2>
      <p className="max-w-lg break-words font-mono text-xs text-[var(--color-fg-secondary)]">
        {message}
      </p>
      <p className="max-w-lg text-sm text-[var(--color-fg-muted)]">
        Nothing was lost — this is a display failure, not a data one. Full details are in
        the browser console.
      </p>
      <div className="flex gap-2">
        <Button variant="ghost" onClick={() => window.location.reload()}>
          Reload
        </Button>
        <Link to="/">
          <Button variant="accent">Back to Cases</Button>
        </Link>
      </div>
    </div>
  );
}
