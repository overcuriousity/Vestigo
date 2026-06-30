import { Link, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Wifi, WifiOff, Activity } from "lucide-react";
import { healthApi } from "@/api/health";
import { JobTray } from "./JobTray";
import { Tooltip } from "@/components/ui/Tooltip";
import { cn } from "@/lib/cn";

function HealthIndicator() {
  const { data, isError } = useQuery({
    queryKey: ["health"],
    queryFn: () => healthApi.check(),
    refetchInterval: 15_000,
    retry: false,
  });

  const connected = !!data && !isError;

  return (
    <Tooltip content={connected ? `API v${data?.version ?? "?"}` : "API unreachable"}>
      <div
        className={cn(
          "flex items-center gap-1.5 rounded px-2 py-1 text-xs transition-base",
          connected
            ? "text-[var(--color-success)]"
            : "text-[var(--color-danger)]",
        )}
      >
        {connected ? <Wifi size={13} /> : <WifiOff size={13} />}
        <span className="hidden sm:inline">{connected ? "Connected" : "Offline"}</span>
      </div>
    </Tooltip>
  );
}

export function TopBar() {
  const location = useLocation();

  const crumbs: { label: string; to: string }[] = [];
  const parts = location.pathname.split("/").filter(Boolean);
  // Build simple breadcrumbs from path segments
  if (parts[0] === "cases") {
    crumbs.push({ label: "Cases", to: "/" });
    if (parts[1]) {
      crumbs.push({ label: parts[1], to: `/cases/${parts[1]}` });
    }
    if (parts[2] === "timelines" && parts[3]) {
      crumbs.push({
        label: parts[3],
        to: `/cases/${parts[1]}/timelines/${parts[3]}`,
      });
    }
  }

  return (
    <header className="relative flex h-11 shrink-0 items-center gap-3 border-b border-[var(--color-border)] bg-[var(--color-bg-surface)] px-4">
      {/* Logo */}
      <Link
        to="/"
        className="flex items-center gap-2 text-[var(--color-fg-primary)] hover:text-[var(--color-accent)] transition-base"
      >
        <Activity size={16} className="text-[var(--color-accent)]" />
        <span className="font-semibold tracking-tight">TraceVector</span>
      </Link>

      {/* Breadcrumb */}
      {crumbs.length > 0 && (
        <nav className="flex items-center gap-1 text-xs text-[var(--color-fg-muted)]">
          <span>/</span>
          {crumbs.map((c, i) => (
            <span key={c.to} className="flex items-center gap-1">
              {i < crumbs.length - 1 ? (
                <>
                  <Link
                    to={c.to}
                    className="font-mono hover:text-[var(--color-fg-primary)] transition-base truncate max-w-[120px]"
                  >
                    {c.label}
                  </Link>
                  <span>/</span>
                </>
              ) : (
                <span className="font-mono text-[var(--color-fg-secondary)] truncate max-w-[160px]">
                  {c.label}
                </span>
              )}
            </span>
          ))}
        </nav>
      )}

      <div className="ml-auto flex items-center gap-2">
        <JobTray />
        <HealthIndicator />
      </div>
    </header>
  );
}
