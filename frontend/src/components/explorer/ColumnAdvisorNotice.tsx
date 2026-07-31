/**
 * The disclosure an analyst reads before AI column suggestions send anything
 * (issue #213).
 *
 * A timeline's opening columns are always derived from its own field
 * statistics, on this machine, and every automatic trigger — ingest, timeline
 * creation, the CLI, the demo build — stops there. Showing the candidate table
 * to a model is a separate, deliberate act, because that table carries real
 * sample values from the case's events. This dialog is where that act is
 * authorized: it names what is sent, where it goes, and which model reads it,
 * before anything has left the machine.
 *
 * Opened by the "Suggest with AI" button in `ColumnPicker`, never on its own —
 * an analyst who has not asked for this is never interrupted by it. Confirming
 * records the opt-in per timeline (`preferences.column_advisor_optin`), which
 * is the granularity at which evidence is actually sent, so the button runs
 * straight through on that timeline afterwards and asks again on the next one.
 *
 * The copy lives here rather than in `lib/guidance.tsx` because it interpolates
 * the resolved endpoint and model; the guidance registry is keyed by
 * `GuidancePanel` ids and everything in it must render without runtime data.
 */
import { useQuery } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { agentApi } from "@/api/agent";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent } from "@/components/ui/Dialog";
import { Spinner } from "@/components/ui/Spinner";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Record the opt-in and run the suggestion. */
  onConfirm: () => void;
  /** True while the opt-in write or the job request is in flight. */
  pending?: boolean;
  /**
   * Which half of the confirm failed, if either. `"save"` is the opt-in write,
   * after which the run is never attempted; `"run"` is the request to start
   * the job, which the saved opt-in outlived. Nothing has left the machine in
   * either case — the advisor only runs server-side, once the job starts — but
   * telling an analyst their opt-in "did not save" when it did is how they end
   * up re-authorizing something they already authorized.
   */
  error?: "save" | "run" | null;
}

export function ColumnAdvisorNotice({ open, onOpenChange, onConfirm, pending, error }: Props) {
  const { data: info } = useQuery({
    queryKey: ["agent-info"],
    queryFn: agentApi.getInfo,
    staleTime: 60_000,
    enabled: open,
  });

  const endpoint = info?.api_base_url ?? "the configured endpoint";
  const model = info?.model ?? "the configured model";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title="Suggest columns with AI"
        description="This timeline's field statistics are shown to the configured language model, which picks the columns it opens on."
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
            Columns are suggested either way — scored from this timeline&rsquo;s own field
            statistics on this machine, with nothing sent anywhere. The model never introduces
            a field of its own; it only reorders and drops what the scorer already surfaced,
            and your own column choice always wins locally.
          </p>
          <p className="text-xs text-[var(--color-fg-muted)]">
            The result is shared with everyone who can see this timeline, and the run is
            recorded in the case audit trail under your name. This covers this timeline only —
            you will be asked again on the next one.
          </p>
        </div>

        {error && (
          <p className="mt-3 text-xs text-[var(--color-danger)]">
            {error === "save"
              ? "That did not save — nothing was sent. Try again."
              : "Your choice was saved, but the suggestion did not start — nothing was sent. Try again."}
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)} disabled={pending}>
            Cancel
          </Button>
          <Button size="sm" onClick={onConfirm} disabled={pending}>
            {pending ? <Spinner size={11} /> : <Sparkles size={11} />}
            Send and suggest
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
