import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Copy, Ban } from "lucide-react";
import { agentTokensApi } from "@/api/agentTokens";
import { Dialog, DialogContent, DialogTrigger } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { Badge } from "@/components/ui/Badge";
import { toast } from "@/stores/toasts";
import { fmtRelative } from "@/lib/time";
import type { Timeline } from "@/api/types";

interface Props {
  caseId: string;
  timeline: Timeline;
}

/** Manage scoped MCP access tokens for external agents (docs/AGENT.md). */
export function AgentTokensDialog({ caseId, timeline }: Props) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [expiresDays, setExpiresDays] = useState("");
  const [freshToken, setFreshToken] = useState<string | null>(null);
  const qc = useQueryClient();
  const queryKey = ["agent-tokens", caseId, timeline.id] as const;

  const { data, isLoading } = useQuery({
    queryKey,
    queryFn: () => agentTokensApi.list(caseId, timeline.id),
    enabled: open,
  });

  const createMutation = useMutation({
    mutationFn: () =>
      agentTokensApi.create(caseId, timeline.id, {
        name: name.trim(),
        ...(expiresDays ? { expires_in_days: Number(expiresDays) } : {}),
      }),
    onSuccess: (created) => {
      setFreshToken(created.token);
      setName("");
      setExpiresDays("");
      void qc.invalidateQueries({ queryKey });
      toast.success("Token created");
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const revokeMutation = useMutation({
    mutationFn: (tokenId: string) => agentTokensApi.revoke(caseId, timeline.id, tokenId),
    onSuccess: () => void qc.invalidateQueries({ queryKey }),
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) setFreshToken(null);
      }}
    >
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon" title="MCP access tokens">
          <KeyRound size={14} />
        </Button>
      </DialogTrigger>
      <DialogContent
        title={`MCP access tokens — ${timeline.name}`}
        description="Tokens let an external MCP client (e.g. Claude Code) investigate this timeline read-only via /mcp. Scope is fixed to this timeline; revocation is immediate."
        className="max-w-xl"
      >
        {freshToken && (
          <div className="mb-3 rounded border border-[var(--color-warning)] p-2 text-xs">
            <div className="mb-1 font-medium">Shown once — store it now:</div>
            <div className="flex items-center gap-2">
              <code className="flex-1 break-all">{freshToken}</code>
              <Button
                variant="ghost"
                size="icon"
                title="Copy"
                onClick={() => {
                  void navigator.clipboard.writeText(freshToken);
                  toast.success("Token copied");
                }}
              >
                <Copy size={12} />
              </Button>
            </div>
            <div className="mt-1 text-[var(--color-fg-muted)]">
              Connect an MCP client to /mcp with Authorization: Bearer &lt;token&gt;.
            </div>
          </div>
        )}
        {isLoading && <Spinner />}
        <div className="space-y-1">
          {data?.tokens.map((t) => (
            <div key={t.id} className="flex items-center gap-2 text-xs">
              <span className="flex-1 truncate">{t.name}</span>
              {t.revoked_at ? (
                <Badge variant="muted">revoked</Badge>
              ) : (
                <>
                  {t.expires_at && <span>expires {fmtRelative(t.expires_at)}</span>}
                  <Button
                    variant="ghost"
                    size="icon"
                    title="Revoke"
                    onClick={() => revokeMutation.mutate(t.id)}
                  >
                    <Ban size={12} />
                  </Button>
                </>
              )}
            </div>
          ))}
          {data && data.tokens.length === 0 && (
            <p className="text-xs text-[var(--color-fg-muted)]">No tokens yet.</p>
          )}
        </div>
        <form
          className="mt-3 flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) createMutation.mutate();
          }}
        >
          <input
            className="flex-1 rounded border border-[var(--color-border)] bg-transparent px-2 py-1 text-xs"
            placeholder="Token name (e.g. claude-code)"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <input
            className="w-24 rounded border border-[var(--color-border)] bg-transparent px-2 py-1 text-xs"
            placeholder="days (opt)"
            inputMode="numeric"
            value={expiresDays}
            onChange={(e) => setExpiresDays(e.target.value.replace(/\D/g, ""))}
          />
          <Button type="submit" size="sm" disabled={!name.trim() || createMutation.isPending}>
            Create
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}
