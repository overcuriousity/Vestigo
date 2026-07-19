/**
 * ProposalCard — renders an agent `propose_annotation` tool call as a
 * confirm/reject-able card: the agent never writes annotations directly,
 * it proposes a tag/comment over a set of events with a rationale, and an
 * analyst decides. Mirrors `FindingCard`'s structure/styling; on confirm,
 * "Open in Explorer" reuses the same `specToEventFilters({ event_ids })` →
 * `onApply` seam FindingCard's "Apply to Explorer" uses.
 */
import { CircleCheck, CircleX, Tag as TagIcon } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { agentApi, specToEventFilters, type AgentProposal } from "@/api/agent";
import { ApiError } from "@/api/client";
import type { EventFilters } from "@/api/types";
import { useUserNames } from "@/hooks/useUserNames";

interface Props {
  caseId: string;
  conversationId: string;
  proposal: AgentProposal;
  onApply: (filters: EventFilters) => void;
}

export function ProposalCard({ caseId, conversationId, proposal, onApply }: Props) {
  const queryClient = useQueryClient();
  const queryKey = ["agent-proposals", caseId, conversationId];

  const confirmMutation = useMutation({
    mutationFn: () => agentApi.confirmProposal(caseId, conversationId, proposal.id),
    onSettled: () => queryClient.invalidateQueries({ queryKey }),
    // A 409 means another tab/analyst already decided this proposal — refetch
    // and render the decided state instead of surfacing an error toast.
    onError: (err) => {
      if (!(err instanceof ApiError) || err.status !== 409) throw err;
    },
  });

  const rejectMutation = useMutation({
    mutationFn: () => agentApi.rejectProposal(caseId, conversationId, proposal.id),
    onSettled: () => queryClient.invalidateQueries({ queryKey }),
    onError: (err) => {
      if (!(err instanceof ApiError) || err.status !== 409) throw err;
    },
  });

  const deciding = confirmMutation.isPending || rejectMutation.isPending;
  const skippedCount = confirmMutation.data?.skipped_event_ids.length ?? 0;
  const userName = useUserNames();

  if (proposal.status === "rejected") {
    return (
      <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg-surface)] p-2 text-xs text-[var(--color-fg-secondary)]">
        <div className="flex items-center gap-1.5">
          <CircleX size={13} className="shrink-0 text-[var(--color-fg-secondary)]" />
          <span>
            Annotation proposal rejected
            {proposal.decided_by ? ` by ${userName(proposal.decided_by)}` : ""}
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-md border border-[var(--color-accent)] bg-[var(--color-accent-dim)] p-2.5 text-xs">
      <div className="flex items-center gap-1.5 font-semibold text-[var(--color-fg-primary)]">
        <TagIcon size={13} className="shrink-0 text-[var(--color-accent)]" />
        <span className="min-w-0 break-words">Annotation proposal</span>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1">
        {proposal.tag && (
          <span className="rounded bg-[var(--color-bg-surface)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--color-fg-primary)]">
            tag: {proposal.tag}
          </span>
        )}
        {proposal.comment && (
          <span className="min-w-0 break-words rounded bg-[var(--color-bg-surface)] px-1.5 py-0.5 text-[10px] text-[var(--color-fg-primary)]">
            {proposal.comment}
          </span>
        )}
        <span className="rounded bg-[var(--color-bg-surface)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--color-fg-primary)]">
          {proposal.events.length} event{proposal.events.length === 1 ? "" : "s"}
        </span>
      </div>
      {proposal.rationale && (
        <p className="mt-1.5 text-[var(--color-fg-secondary)]">{proposal.rationale}</p>
      )}

      {proposal.status === "confirmed" ? (
        <div className="mt-2 flex items-center justify-between gap-2">
          <span className="flex items-center gap-1 text-[var(--color-success)]">
            <CircleCheck size={13} className="shrink-0" />
            written{proposal.decided_by ? ` by ${userName(proposal.decided_by)}` : ""}
          </span>
          <Button
            variant="accent"
            size="sm"
            onClick={() =>
              onApply(specToEventFilters({ event_ids: proposal.events.map((e) => e.event_id) }))
            }
          >
            Open in Explorer
          </Button>
        </div>
      ) : (
        <div className="mt-2 flex items-center justify-end gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={deciding}
            onClick={() => rejectMutation.mutate()}
          >
            {rejectMutation.isPending ? <Spinner size={12} /> : "Reject"}
          </Button>
          <Button
            variant="accent"
            size="sm"
            disabled={deciding}
            onClick={() => confirmMutation.mutate()}
          >
            {confirmMutation.isPending ? <Spinner size={12} /> : "Confirm"}
          </Button>
        </div>
      )}
      {skippedCount > 0 && (
        <p className="mt-1.5 text-[var(--color-fg-secondary)]">
          {skippedCount} event{skippedCount === 1 ? "" : "s"} no longer resolvable — skipped
        </p>
      )}
    </div>
  );
}
