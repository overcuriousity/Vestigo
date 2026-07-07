import { Link, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Wifi,
  WifiOff,
  Activity,
  Sun,
  Moon,
  Rows2,
  Rows3,
  UserCircle,
  Settings as SettingsIcon,
  ShieldCheck,
  LogOut,
} from "lucide-react";
import { useHealth } from "@/api/health";
import { authApi } from "@/api/auth";
import { JobTray } from "./JobTray";
import { Tooltip } from "@/components/ui/Tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/DropdownMenu";
import { cn } from "@/lib/cn";
import { useAuthStore } from "@/stores/auth";
import { useThemeStore } from "@/stores/theme";
import { useUiStore } from "@/stores/ui";

function ThemeToggle() {
  const theme = useThemeStore((s) => s.theme);
  const toggleTheme = useThemeStore((s) => s.toggleTheme);
  const isDark = theme === "dark";

  return (
    <Tooltip content={isDark ? "Switch to light theme" : "Switch to dark theme"}>
      <button
        type="button"
        onClick={toggleTheme}
        className="flex items-center justify-center rounded p-1.5 text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-fg-primary)] transition-base"
        aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
      >
        {isDark ? <Sun size={14} /> : <Moon size={14} />}
      </button>
    </Tooltip>
  );
}

function DensityToggle() {
  const density = useUiStore((s) => s.density);
  const setDensity = useUiStore((s) => s.setDensity);
  const isCompact = density === "compact";

  return (
    <Tooltip content={isCompact ? "Switch to comfortable density" : "Switch to compact density"}>
      <button
        type="button"
        onClick={() => setDensity(isCompact ? "comfortable" : "compact")}
        className="flex items-center justify-center rounded p-1.5 text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-fg-primary)] transition-base"
        aria-label={isCompact ? "Switch to comfortable density" : "Switch to compact density"}
      >
        {isCompact ? <Rows3 size={14} /> : <Rows2 size={14} />}
      </button>
    </Tooltip>
  );
}

function HealthIndicator() {
  const { data, isError } = useHealth();

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

function UserMenu() {
  const user = useAuthStore((s) => s.user);
  const clear = useAuthStore((s) => s.clear);
  const navigate = useNavigate();
  const qc = useQueryClient();

  const { mutate: logout } = useMutation({
    mutationFn: authApi.logout,
    onSuccess: () => {
      clear();
      qc.setQueryData(["auth", "me"], null);
      navigate("/login");
    },
  });

  if (!user) return null;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="flex items-center gap-1.5 rounded px-2 py-1 text-xs text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-fg-primary)] transition-base"
        >
          <UserCircle size={16} />
          <span className="hidden sm:inline">{user.display_name || user.username}</span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuLabel>{user.username}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => navigate("/settings")}>
          <SettingsIcon size={14} /> Settings
        </DropdownMenuItem>
        {user.is_admin && (
          <DropdownMenuItem onSelect={() => navigate("/admin")}>
            <ShieldCheck size={14} /> Admin
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => logout()}>
          <LogOut size={14} /> Log out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
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
    <header className="flex h-11 shrink-0 items-center gap-3 border-b border-[var(--color-border)] bg-[var(--color-bg-surface)] px-4">
      {/* Logo */}
      <Link
        to="/"
        className="flex items-center gap-2 text-[var(--color-fg-primary)] hover:text-[var(--color-accent)] transition-base"
      >
        <Activity size={16} className="text-[var(--color-accent)]" />
        <span className="font-semibold tracking-tight">TraceSignal</span>
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
        <DensityToggle />
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  );
}
