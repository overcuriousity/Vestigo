/**
 * NewConversationDialog — the gate in front of every new agent conversation.
 *
 * Two jobs, both OPSEC-driven:
 * 1. A prominent notice stating exactly where evidence data goes: the
 *    configured LLM endpoint URL and model (from GET /api/agent/info).
 *    Shown every time — there is deliberately no "don't show again".
 * 2. Per-chat tool selection: which investigation tools this conversation's
 *    agent may use. Admin-disabled tools are hard-denied (shown but locked);
 *    the user's own defaults pre-populate the checkboxes and can be saved
 *    back to their account.
 */
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertTriangle, Check, Sparkles } from "lucide-react";

import { agentApi, type AgentToolInfo } from "@/api/agent";
import { Dialog, DialogContent, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Checkbox";
import { Spinner } from "@/components/ui/Spinner";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the tools the user unchecked (the per-chat deny list). */
  onCreate: (disabledTools: string[]) => void;
  creating?: boolean;
  /** Create-conversation failure from the parent, shown inline. */
  error?: string | null;
}

/** Tools whose absence changes the sandbox+apply workflow, not just coverage. */
const WORKFLOW_TOOLS = new Set(["propose_finding", "propose_annotation"]);

function ToolRow({
  tool,
  checked,
  onToggle,
}: {
  tool: AgentToolInfo;
  checked: boolean;
  onToggle: (checked: boolean) => void;
}) {
  return (
    <label
      className={`flex cursor-pointer items-start gap-2 rounded px-1.5 py-1 hover:bg-[var(--color-bg-elevated)] ${
        tool.admin_disabled ? "cursor-not-allowed opacity-50" : ""
      }`}
    >
      <Checkbox
        checked={checked}
        disabled={tool.admin_disabled}
        onCheckedChange={(v) => onToggle(v === true)}
        className="mt-0.5"
      />
      <span className="min-w-0 text-xs">
        <span className="font-mono">{tool.name}</span>
        {tool.admin_disabled && (
          <span className="ml-1.5 rounded bg-[var(--color-bg-elevated)] px-1 py-px text-[10px] text-[var(--color-fg-secondary)]">
            disabled by admin
          </span>
        )}
        {tool.embeddings_gated && (
          <span className="ml-1.5 rounded bg-[var(--color-bg-elevated)] px-1 py-px text-[10px] text-[var(--color-fg-secondary)]">
            needs embeddings
          </span>
        )}
        <span className="block text-[11px] text-[var(--color-fg-secondary)]">
          {tool.description}
        </span>
        {WORKFLOW_TOOLS.has(tool.name) && !checked && !tool.admin_disabled && (
          <span className="block text-[11px] text-[var(--color-warning)]">
            Disabling this removes the {tool.name === "propose_finding" ? "finding" : "annotation"}{" "}
            proposal cards from this chat.
          </span>
        )}
      </span>
    </label>
  );
}

export function NewConversationDialog({ open, onOpenChange, onCreate, creating, error }: Props) {
  const infoQuery = useQuery({
    queryKey: ["agent-info"],
    queryFn: agentApi.getInfo,
    enabled: open,
  });
  const info = infoQuery.data;

  // Checked = enabled for this chat. Initialized from the user's saved
  // defaults each time the dialog opens.
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  useEffect(() => {
    if (!open || !info) return;
    const initial: Record<string, boolean> = {};
    for (const t of info.tools) {
      initial[t.name] = !t.admin_disabled && !info.user_disabled_tools.includes(t.name);
    }
    setChecked(initial);
  }, [open, info]);

  const disabledTools = useMemo(() => {
    if (!info) return [];
    return info.tools
      .filter((t) => !t.admin_disabled && checked[t.name] === false)
      .map((t) => t.name);
  }, [info, checked]);

  const savePrefs = useMutation({
    mutationFn: () => agentApi.updatePreferences(disabledTools),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title="Start agent conversation"
        description="Review where your evidence goes and which tools the agent may use."
      >
        <div className="space-y-3">
          {/* OPSEC notice — always shown, values live from the backend. */}
          <div className="flex gap-2 rounded-md border border-[var(--color-warning)] bg-[var(--color-warning)]/10 p-2.5">
            <AlertTriangle
              size={16}
              className="mt-0.5 shrink-0 text-[var(--color-warning)]"
            />
            <p className="text-xs leading-relaxed">
              <span className="font-semibold">Evidence leaves Vestigo.</span> Event data from this
              timeline — including anything the agent's tools return — is sent to{" "}
              <span className="break-all font-mono font-semibold">
                {info?.api_base_url ?? "the configured LLM endpoint"}
              </span>{" "}
              and processed by model{" "}
              <span className="font-mono font-semibold">{info?.model ?? "(unknown)"}</span>. Make
              sure this is acceptable for the sensitivity of this case before continuing.
            </p>
          </div>

          {/* Tool selection */}
          <div>
            <p className="mb-1 text-xs font-semibold text-[var(--color-fg-primary)]">
              Tools for this conversation
            </p>
            {infoQuery.isLoading && (
              <div className="flex items-center gap-2 px-1 py-3 text-xs text-[var(--color-fg-secondary)]">
                <Spinner size={13} /> Loading tool catalog…
              </div>
            )}
            {infoQuery.isError && (
              <p className="px-1 py-2 text-xs text-[var(--color-danger)]">
                Could not load the agent configuration.
              </p>
            )}
            {info && (
              <div className="max-h-56 space-y-0.5 overflow-y-auto rounded border border-[var(--color-border)] p-1.5">
                {info.tools.map((t) => (
                  <ToolRow
                    key={t.name}
                    tool={t}
                    checked={checked[t.name] ?? false}
                    onToggle={(v) => setChecked((prev) => ({ ...prev, [t.name]: v }))}
                  />
                ))}
              </div>
            )}
          </div>

          {error && (
            <p className="text-xs text-[var(--color-danger)]">
              Could not start the conversation: {error}
            </p>
          )}
          {savePrefs.isError && (
            <p className="text-xs text-[var(--color-danger)]">Saving your defaults failed.</p>
          )}

          <div className="flex items-center justify-between gap-2">
            <Button
              variant="ghost"
              size="sm"
              disabled={!info || savePrefs.isPending}
              onClick={() => savePrefs.mutate()}
            >
              {savePrefs.isSuccess ? (
                <>
                  <Check size={13} /> Saved
                </>
              ) : (
                "Save as my defaults"
              )}
            </Button>
            <div className="flex gap-2">
              <DialogClose asChild>
                <Button variant="ghost" size="sm">
                  Cancel
                </Button>
              </DialogClose>
              <Button
                variant="accent"
                size="sm"
                disabled={!info || creating}
                onClick={() => onCreate(disabledTools)}
              >
                {creating ? (
                  <>
                    <Spinner size={13} /> Starting…
                  </>
                ) : (
                  <>
                    <Sparkles size={13} /> Start conversation
                  </>
                )}
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
