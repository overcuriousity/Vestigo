/**
 * ToolSelectorPopover — per-chat tool toggles for the next conversation,
 * reachable from a button in the input toolbar instead of a blocking modal.
 * The OPSEC notice itself lives in AgentPanel's empty state, not here —
 * this popover only controls which tools the upcoming conversation may use.
 */
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Check, Settings2 } from "lucide-react";

import { agentApi } from "@/api/agent";
import {
  Popover,
  PopoverTrigger,
  PopoverContent,
} from "@/components/ui/Popover";
import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Checkbox";
import { Spinner } from "@/components/ui/Spinner";

/** Tools whose absence changes the sandbox+apply workflow, not just coverage. */
const WORKFLOW_TOOLS = new Set(["propose_finding", "propose_annotation"]);

interface Props {
  /** The per-chat deny list: for the next conversation to be created, or the
   * active conversation's own set once one exists. */
  disabledTools: string[];
  onChange: (disabledTools: string[]) => void;
  /** Seed from the user's saved defaults on mount. Only correct for a
   * not-yet-created conversation: an existing one already carries its own
   * set, and seeding over it would silently replace the analyst's actual
   * restriction with their defaults — and persist that via the caller. */
  seedFromDefaults?: boolean;
}

export function ToolSelectorPopover({
  disabledTools,
  onChange,
  seedFromDefaults = true,
}: Props) {
  const [open, setOpen] = useState(false);
  const infoQuery = useQuery({
    queryKey: ["agent-info"],
    queryFn: agentApi.getInfo,
  });
  const info = infoQuery.data;

  // Seed the deny list from the user's saved defaults exactly once per mount
  // — after that, onToggle below is the sole source of truth.
  const seededRef = useRef(false);
  useEffect(() => {
    if (!info || seededRef.current || !seedFromDefaults) return;
    seededRef.current = true;
    const initial = info.tools
      .filter(
        (t) => !t.admin_disabled && info.user_disabled_tools.includes(t.name),
      )
      .map((t) => t.name);
    if (initial.length > 0) onChange(initial);
  }, [info, onChange, seedFromDefaults]);

  const savePrefs = useMutation({
    mutationFn: () => agentApi.updatePreferences(disabledTools),
  });

  const toggle = (name: string, checked: boolean) => {
    onChange(
      checked
        ? disabledTools.filter((t) => t !== name)
        : [...disabledTools, name],
    );
  };

  /**
   * Presets (A13). Tool schemas are resent with every model request, so on a
   * small-context local model the full catalog can eat half the window before
   * the conversation starts. "Core" keeps the tools the investigation cycle
   * needs and denies the rest — a disabled tool is removed from the request
   * entirely, so this reclaims context rather than just tidying the list.
   *
   * Admin-disabled tools are left out of the deny list: they are already
   * denied server-side, and naming them here would be redundant state.
   *
   * There is deliberately no "none" preset — an agent with no tools cannot
   * measure anything, and every claim it made would violate the evidence
   * rule in the system prompt. Individual tools stay togglable below.
   */
  const applyPreset = (preset: "core" | "all") => {
    if (!info) return;
    if (preset === "all") return onChange([]);
    onChange(
      info.tools
        .filter((t) => !t.admin_disabled && t.tier !== "core")
        .map((t) => t.name),
    );
  };

  // Only offer "Core" if the backend actually tiers its catalog.
  const hasTiers = !!info?.tools.some((t) => t.tier === "core");

  const enabledCount = info
    ? info.tools.filter(
        (t) => !t.admin_disabled && !disabledTools.includes(t.name),
      ).length
    : 0;
  const totalCount = info
    ? info.tools.filter((t) => !t.admin_disabled).length
    : 0;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 gap-1 px-1.5 text-[11px]"
        >
          <Settings2 size={12} />
          Tools{info ? ` (${enabledCount}/${totalCount})` : ""}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-72 space-y-2 p-2.5">
        <div className="flex items-baseline justify-between gap-2">
          <p className="text-[11px] font-semibold text-[var(--color-fg-primary)]">
            Tools for the next conversation
          </p>
          {info && (
            <div className="flex items-center gap-1">
              {hasTiers && (
                <button
                  type="button"
                  onClick={() => applyPreset("core")}
                  title="Just the tools the investigation cycle needs — smaller prompt, for small-context models"
                  className="rounded px-1 py-px text-[10px] text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-elevated)] hover:text-[var(--color-fg-primary)]"
                >
                  Core
                </button>
              )}
              <button
                type="button"
                onClick={() => applyPreset("all")}
                className="rounded px-1 py-px text-[10px] text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-elevated)] hover:text-[var(--color-fg-primary)]"
              >
                All
              </button>
            </div>
          )}
        </div>
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
            {info.tools.map((t) => {
              const checked =
                !t.admin_disabled && !disabledTools.includes(t.name);
              return (
                <label
                  key={t.name}
                  className={`flex cursor-pointer items-start gap-2 rounded px-1.5 py-1 hover:bg-[var(--color-bg-elevated)] ${
                    t.admin_disabled ? "cursor-not-allowed opacity-50" : ""
                  }`}
                >
                  <Checkbox
                    checked={checked}
                    disabled={t.admin_disabled}
                    onCheckedChange={(v) => toggle(t.name, v === true)}
                    className="mt-0.5"
                  />
                  <span className="min-w-0 text-xs">
                    <span className="font-mono">{t.name}</span>
                    {t.admin_disabled && (
                      <span className="ml-1.5 rounded bg-[var(--color-bg-elevated)] px-1 py-px text-[10px] text-[var(--color-fg-secondary)]">
                        disabled by admin
                      </span>
                    )}
                    {t.embeddings_gated && (
                      <span className="ml-1.5 rounded bg-[var(--color-bg-elevated)] px-1 py-px text-[10px] text-[var(--color-fg-secondary)]">
                        needs embeddings
                      </span>
                    )}
                    <span className="block text-[11px] text-[var(--color-fg-secondary)]">
                      {t.description}
                    </span>
                    {WORKFLOW_TOOLS.has(t.name) &&
                      !checked &&
                      !t.admin_disabled && (
                        <span className="block text-[11px] text-[var(--color-warning)]">
                          Disabling this removes the{" "}
                          {t.name === "propose_finding"
                            ? "finding"
                            : "annotation"}{" "}
                          proposal cards from this chat.
                        </span>
                      )}
                  </span>
                </label>
              );
            })}
          </div>
        )}
        {savePrefs.isError && (
          <p className="text-xs text-[var(--color-danger)]">
            Saving your defaults failed.
          </p>
        )}
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
      </PopoverContent>
    </Popover>
  );
}
