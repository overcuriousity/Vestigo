import { NavLink, Outlet } from "react-router-dom";
import { ShieldCheck } from "lucide-react";
import { cn } from "@/lib/cn";

const TABS = [
  { to: "/admin/users", label: "Users" },
  { to: "/admin/teams", label: "Teams" },
  { to: "/admin/audit", label: "Audit log" },
  { to: "/admin/enrichers", label: "Enrichers" },
  { to: "/admin/agent", label: "Agent" },
  { to: "/admin/settings", label: "Settings" },
];

export function AdminLayout() {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl px-6 py-8">
        <div className="mb-6 flex items-center gap-3">
          <ShieldCheck size={26} className="text-[var(--color-accent)]" />
          <div>
            <h1 className="text-xl font-semibold text-[var(--color-fg-primary)]">
              Administration
            </h1>
            <p className="mt-0.5 text-sm text-[var(--color-fg-muted)]">
              Manage users, investigation teams, the audit trail, and instance configuration.
            </p>
          </div>
        </div>

        <nav className="mb-6 flex gap-1 border-b border-[var(--color-border-strong)]">
          {TABS.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              className={({ isActive }) =>
                cn(
                  "border-b-2 px-3 py-2 text-sm font-medium transition-base",
                  isActive
                    ? "border-[var(--color-accent)] text-[var(--color-fg-primary)]"
                    : "border-transparent text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]",
                )
              }
            >
              {tab.label}
            </NavLink>
          ))}
        </nav>

        <Outlet />
      </div>
    </div>
  );
}
