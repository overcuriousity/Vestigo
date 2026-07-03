import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Download, Settings as SettingsIcon } from "lucide-react";
import { authApi } from "@/api/auth";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { usePasswordChangeForm } from "@/hooks/usePasswordChangeForm";
import { useAuthStore } from "@/stores/auth";
import { useInvalidateCurrentUser } from "@/hooks/useCurrentUser";

export function SettingsPage() {
  const user = useAuthStore((s) => s.user);
  const setUser = useAuthStore((s) => s.setUser);
  const invalidate = useInvalidateCurrentUser();

  const [username, setUsername] = useState(user?.username ?? "");
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const {
    currentPassword,
    setCurrentPassword,
    newPassword,
    setNewPassword,
    confirmPassword,
    setConfirmPassword,
    mismatch: passwordMismatch,
    canSubmit: canSubmitPassword,
    mutation: password,
  } = usePasswordChangeForm();

  const profile = useMutation({
    mutationFn: () => authApi.updateProfile({ username: username.trim(), display_name: displayName.trim() }),
    onSuccess: (u) => {
      setUser(u);
      invalidate();
    },
  });

  if (!user) return null;

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-2xl px-6 py-8">
        <div className="mb-8 flex items-start gap-4">
          <SettingsIcon size={28} className="mt-0.5 shrink-0 text-[var(--color-accent)]" />
          <div>
            <h1 className="text-xl font-semibold text-[var(--color-fg-primary)]">Settings</h1>
            <p className="mt-1 text-sm text-[var(--color-fg-muted)]">
              Manage your account. {user.teams?.length ? (
                <>
                  Member of: {user.teams.map((t) => `${t.name} (${t.role})`).join(", ")}.
                </>
              ) : (
                "You aren't on any team — you only see cases you personally created."
              )}
            </p>
          </div>
        </div>

        {/* Profile */}
        <section className="mb-8 rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-bg-elevated)] p-5">
          <h2 className="mb-4 text-sm font-semibold text-[var(--color-fg-primary)]">Profile</h2>
          <div className="space-y-3">
            <div>
              <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">Username</label>
              <Input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                disabled={user.auth_provider === "oidc"}
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
                Display name
              </label>
              <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
            </div>
            {profile.error && (
              <p className="text-xs text-[var(--color-danger)]">
                {profile.error instanceof ApiError ? profile.error.message : "Something went wrong."}
              </p>
            )}
            <div className="flex justify-end">
              <Button
                variant="accent"
                size="sm"
                disabled={!username.trim() || profile.isPending}
                onClick={() => profile.mutate()}
              >
                {profile.isPending ? "Saving..." : "Save profile"}
              </Button>
            </div>
          </div>
        </section>

        {/* Password */}
        {user.auth_provider === "local" && (
          <section className="mb-8 rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-bg-elevated)] p-5">
            <h2 className="mb-4 text-sm font-semibold text-[var(--color-fg-primary)]">
              Change password
            </h2>
            <div className="space-y-3">
              <div>
                <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
                  Current password
                </label>
                <Input
                  type="password"
                  value={currentPassword}
                  onChange={(e) => setCurrentPassword(e.target.value)}
                  autoComplete="current-password"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
                  New password (min. 8 characters)
                </label>
                <Input
                  type="password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  autoComplete="new-password"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
                  Confirm new password
                </label>
                <Input
                  type="password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  autoComplete="new-password"
                />
              </div>
              {passwordMismatch && (
                <p className="text-xs text-[var(--color-danger)]">Passwords don't match.</p>
              )}
              {password.error && (
                <p className="text-xs text-[var(--color-danger)]">
                  {password.error instanceof ApiError
                    ? password.error.message
                    : "Something went wrong."}
                </p>
              )}
              <div className="flex justify-end">
                <Button
                  variant="accent"
                  size="sm"
                  disabled={!canSubmitPassword || password.isPending}
                  onClick={() => password.mutate()}
                >
                  {password.isPending ? "Changing..." : "Change password"}
                </Button>
              </div>
            </div>
          </section>
        )}

        {/* Audit trail */}
        <section className="rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-bg-elevated)] p-5">
          <h2 className="mb-2 text-sm font-semibold text-[var(--color-fg-primary)]">
            Your audit trail
          </h2>
          <p className="mb-4 text-sm text-[var(--color-fg-muted)]">
            Download a record of everything you've done in TraceSignal — useful for reproducing
            or documenting your own investigative steps.
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={async () => {
              const blob = await authApi.downloadMyAudit("csv");
              const url = URL.createObjectURL(blob);
              const a = document.createElement("a");
              a.href = url;
              a.download = `audit-${user.username}.csv`;
              a.click();
              URL.revokeObjectURL(url);
            }}
          >
            <Download size={14} /> Download my audit trail (CSV)
          </Button>
        </section>
      </div>
    </div>
  );
}
