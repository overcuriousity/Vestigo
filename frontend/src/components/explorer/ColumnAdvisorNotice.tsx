/**
 * One-time disclosure for the LLM half of column suggestions (issue #213).
 *
 * A timeline's opening columns are always derived from its own field
 * statistics, on this machine. When `column_recommend_mode` is `auto`, the
 * candidate table is additionally shown to the configured model — and that
 * table carries real sample values from the case's events. That is egress from
 * a forensic tool, so it is opt-in, and this dialog is where the opting
 * happens: it names what is sent, where it goes, and which model reads it,
 * before anything has left the machine.
 *
 * Shown once per user (`preferences.column_advisor_notice_ack`), never again
 * on any machine — a notice that reappears is a notice people dismiss unread.
 *
 * The copy lives here rather than in `lib/guidance.tsx` because it interpolates
 * the resolved endpoint and model; the guidance registry is keyed by
 * `GuidancePanel` ids and everything in it must render without runtime data.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { agentApi } from "@/api/agent";
import { authApi } from "@/api/auth";
import { settingsApi } from "@/api/settings";
import type { ColumnRecommendMode } from "@/api/types";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent } from "@/components/ui/Dialog";
import { Spinner } from "@/components/ui/Spinner";
import { useAuthStore } from "@/stores/auth";

/** Preference key set once the user has seen this dialog. */
export const COLUMN_ADVISOR_ACK = "column_advisor_notice_ack";

interface Props {
  /** This instance's suggestion mode, from `/api/health`. */
  mode: ColumnRecommendMode;
}

export function ColumnAdvisorNotice({ mode }: Props) {
  const user = useAuthStore((s) => s.user);
  const setUser = useAuthStore((s) => s.setUser);
  const queryClient = useQueryClient();
  // Closing is driven by the persisted acknowledgement, not by the click: a
  // dialog that vanishes while the write fails would come back on the next
  // visit with no explanation. Dismissing without persisting (Escape, overlay)
  // is still allowed — it just means "ask me again".
  const [dismissed, setDismissed] = useState(false);

  const { data: info } = useQuery({
    queryKey: ["agent-info"],
    queryFn: agentApi.getInfo,
    staleTime: 60_000,
  });

  // Admins only: reading it as a non-admin is a guaranteed 403, and the answer
  // (whether the deployment pinned the mode) is only actionable for an admin.
  const { data: settings } = useQuery({
    queryKey: ["admin", "settings"],
    queryFn: settingsApi.get,
    enabled: !!user?.is_admin,
    staleTime: 60_000,
  });
  const spec = settings?.settings.find((s) => s.field === "column_recommend_mode");
  const pinnedByEnv = spec?.source === "env";
  const canEnable = !!user?.is_admin && !!spec && spec.editable;

  const acknowledge = useMutation({
    mutationFn: async (enable: boolean) => {
      if (enable) {
        await settingsApi.update({ column_recommend_mode: "auto" });
      }
      return authApi.updatePreferences({ [COLUMN_ADVISOR_ACK]: true });
    },
    onSuccess: (updated, enable) => {
      setUser(updated);
      // Written in place rather than invalidated: a refetch would briefly
      // re-expose the stale user (ack still absent) and re-open this dialog.
      queryClient.setQueryData(["auth", "me"], updated);
      if (enable) {
        queryClient.invalidateQueries({ queryKey: ["admin", "settings"] });
        queryClient.invalidateQueries({ queryKey: ["health"] });
      }
      setDismissed(true);
    },
  });

  if (!user || dismissed || mode === "off") return null;
  if (user.preferences?.[COLUMN_ADVISOR_ACK]) return null;

  const endpoint = info?.api_base_url ?? "the configured endpoint";
  const model = info?.model ?? "the configured model";

  return (
    <Dialog open onOpenChange={(open) => !open && setDismissed(true)}>
      <DialogContent
        title="AI column suggestions"
        description="Vestigo can ask a language model which columns this timeline should open on."
      >
        <div className="space-y-4 text-sm text-[var(--color-fg-secondary)]">
          <section className="space-y-1">
            <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--color-fg-muted)]">
              What is sent
            </h3>
            <ul className="list-disc space-y-0.5 pl-4">
              <li>Each candidate field&rsquo;s name and coverage statistics</li>
              <li>Up to three real sample values per field, 40 characters each</li>
              <li>At most 20 fields per request</li>
              <li>No event rows, no case or source identifiers, no credentials</li>
            </ul>
          </section>

          <section className="space-y-1">
            <h3 className="text-xs font-medium uppercase tracking-wider text-[var(--color-fg-muted)]">
              Where it goes
            </h3>
            <p className="font-mono text-xs break-all">{endpoint}</p>
            <p className="text-xs">
              {model}
              {info?.provider ? ` (${info.provider})` : ""}
            </p>
          </section>

          <p className="text-xs text-[var(--color-fg-muted)]">
            Without this, columns are still suggested — scored from the timeline&rsquo;s own
            field statistics on this machine, with nothing sent anywhere. The model never
            introduces a field of its own; it only reorders and drops what the scorer already
            surfaced, and your own column choice always wins locally.
          </p>

          {pinnedByEnv && (
            <p className="text-xs text-[var(--color-fg-muted)]">
              This deployment pins the setting through{" "}
              <code className="font-mono">VESTIGO_COLUMN_RECOMMEND_MODE</code>, so it cannot be
              changed from here.
            </p>
          )}
          {!user.is_admin && (
            <p className="text-xs text-[var(--color-fg-muted)]">
              An administrator controls this in Settings → Explorer. This instance is currently
              set to <span className="font-mono">{mode}</span>.
            </p>
          )}
        </div>

        {acknowledge.isError && (
          <p className="mt-3 text-xs text-[var(--color-danger)]">
            That did not save — this notice will appear again next time.
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          {canEnable && mode !== "auto" ? (
            <>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => acknowledge.mutate(false)}
                disabled={acknowledge.isPending}
              >
                Keep statistics-only
              </Button>
              <Button
                size="sm"
                onClick={() => acknowledge.mutate(true)}
                disabled={acknowledge.isPending}
              >
                {acknowledge.isPending ? <Spinner size={11} /> : <Sparkles size={11} />}
                Enable
              </Button>
            </>
          ) : (
            <Button
              size="sm"
              onClick={() => acknowledge.mutate(false)}
              disabled={acknowledge.isPending}
            >
              Got it
            </Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
