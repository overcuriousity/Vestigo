import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";
import { ShieldAlert } from "lucide-react";
import { authApi } from "@/api/auth";
import { ApiError } from "@/api/client";
import { useHealth } from "@/api/health";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { useAuthStore } from "@/stores/auth";

export function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const setUser = useAuthStore((s) => s.setUser);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: Location })?.from?.pathname ?? "/";

  // The health endpoint is unauthenticated and reflects the server's live
  // TS_OIDC_ENABLED setting, unlike a build-time Vite env var, which
  // tsig-web's auto-build never sets — see docs/reviews/PR7-auth-rbac-audit-review.md #6.
  const { data: health } = useHealth();
  const oidcEnabled = health?.oidc_enabled ?? false;

  const { mutate, isPending, error } = useMutation({
    mutationFn: () => authApi.login(username, password),
    onSuccess: (user) => {
      setUser(user);
      // Seed the cache with the fresh user rather than invalidating: a stale
      // cached 401 would otherwise survive until the refetch settles and sign
      // us right back out (see useCurrentUser).
      queryClient.setQueryData(["auth", "me"], user);
      navigate(from, { replace: true });
    },
  });

  return (
    <div className="flex h-svh items-center justify-center bg-[var(--color-bg-base)] px-4">
      <div className="w-full max-w-sm rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-bg-elevated)] p-6">
        <div className="mb-1 flex items-center gap-2">
          <ShieldAlert size={22} className="text-[var(--color-accent)]" />
          <h1 className="text-lg font-semibold text-[var(--color-fg-primary)]">TraceSignal</h1>
        </div>
        <p className="mb-5 text-sm text-[var(--color-fg-muted)]">Sign in to continue.</p>
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (username && password) mutate();
          }}
        >
          <label className="flex flex-col gap-1 text-xs text-[var(--color-fg-secondary)]">
            Username
            <Input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              autoComplete="username"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-[var(--color-fg-secondary)]">
            Password
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
            />
          </label>
          {error && (
            <p className="text-xs text-[var(--color-danger)]">
              {error instanceof ApiError ? error.message : "Something went wrong."}
            </p>
          )}
          <Button
            type="submit"
            variant="accent"
            disabled={!username || !password || isPending}
            className="mt-2"
          >
            {isPending ? "Signing in..." : "Sign in"}
          </Button>
        </form>
        {oidcEnabled && (
          <>
            <div className="my-4 flex items-center gap-2 text-xs text-[var(--color-fg-muted)]">
              <div className="h-px flex-1 bg-[var(--color-border-strong)]" />
              or
              <div className="h-px flex-1 bg-[var(--color-border-strong)]" />
            </div>
            <Button
              variant="outline"
              className="w-full"
              onClick={() => {
                window.location.href = authApi.oidcLoginUrl();
              }}
            >
              Sign in with SSO
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
