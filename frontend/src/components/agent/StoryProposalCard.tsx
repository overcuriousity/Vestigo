/**
 * StoryProposalCard — renders an agent `propose_story` call as a
 * confirm/reject-able card.
 *
 * Same sandbox + apply model as the block card: the agent drafts, the
 * analyst's click is the write. This one exists because a case with no
 * stories was a dead end — the agent could be asked for a report and had no
 * way to make the document to put it in.
 *
 * Deliberately does not chain: confirming creates an *empty* story, and the
 * agent proposes blocks into it on the next turn. A story and its contents
 * confirmed in one click would hide the block-level sign-off the subsystem is
 * built on.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { BookPlus, CircleCheck, CircleX } from "lucide-react";
import { agentApi, type AgentProposal, type StoryProposalPayload } from "@/api/agent";
import { ApiError } from "@/api/client";
import { storiesApi } from "@/api/stories";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { useUserNames } from "@/hooks/useUserNames";
import { toast } from "@/stores/toasts";

interface Props {
  caseId: string;
  conversationId: string;
  proposal: AgentProposal;
}

export function StoryProposalCard({ caseId, conversationId, proposal }: Props) {
  const queryClient = useQueryClient();
  const queryKey = ["agent-proposals", caseId, conversationId];
  const payload = proposal.payload as StoryProposalPayload | null;
  const userName = useUserNames();

  const onDecideError = (err: Error) => {
    // A 409 means another tab/analyst decided first — refetch and render the
    // decided state rather than surfacing an error.
    if (err instanceof ApiError && err.status === 409) return;
    toast.error("Proposal update failed", err.message);
  };

  const confirmMutation = useMutation({
    mutationFn: () => agentApi.confirmProposal(caseId, conversationId, proposal.id),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["stories", caseId] });
      if (res.applied === false && res.reason) {
        toast.error("The story was not created", res.reason);
      }
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey }),
    onError: onDecideError,
  });

  const rejectMutation = useMutation({
    mutationFn: () => agentApi.rejectProposal(caseId, conversationId, proposal.id),
    onSettled: () => queryClient.invalidateQueries({ queryKey }),
    onError: onDecideError,
  });

  const deciding = confirmMutation.isPending || rejectMutation.isPending;

  // Resolved by title rather than kept from the confirm response: a confirmed
  // proposal re-renders from the persisted transcript on the next page load,
  // where the mutation's result is gone but the story it created is not. The
  // propose-time uniqueness check is what makes the title a usable handle.
  const { data: stories } = useQuery({
    queryKey: ["stories", caseId],
    queryFn: () => storiesApi.list(caseId),
    enabled: proposal.status === "confirmed",
  });
  const created =
    stories?.find(
      (s) => s.title.trim().toLowerCase() === (payload?.title ?? "").trim().toLowerCase(),
    ) ?? null;

  if (proposal.status === "rejected") {
    return (
      <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg-surface)] p-2 text-xs text-[var(--color-fg-secondary)]">
        <div className="flex items-center gap-1.5">
          <CircleX size={13} className="shrink-0" />
          <span>
            Story proposal rejected
            {proposal.decided_by ? ` by ${userName(proposal.decided_by)}` : ""}
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-md border border-[var(--color-accent)] bg-[var(--color-accent-dim)] p-2.5 text-xs">
      <div className="flex items-center gap-1.5 font-semibold text-[var(--color-fg-primary)]">
        <BookPlus size={13} className="shrink-0 text-[var(--color-accent)]" />
        <span className="min-w-0 break-words">New story proposal</span>
      </div>

      <div className="mt-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)] p-2">
        <p className="font-medium text-[var(--color-fg-primary)]">
          {payload?.title ?? "untitled"}
        </p>
        {payload?.description && (
          <p className="mt-0.5 text-[var(--color-fg-secondary)]">{payload.description}</p>
        )}
        <p className="mt-1 text-[var(--color-fg-muted)]">
          Confirming creates an empty story. The agent proposes its blocks separately, and you
          confirm each one.
        </p>
      </div>

      {proposal.rationale && (
        <p className="mt-1.5 text-[var(--color-fg-secondary)]">{proposal.rationale}</p>
      )}

      {proposal.status === "confirmed" ? (
        <div className="mt-2 flex items-center justify-between gap-2">
          <span className="flex items-center gap-1 text-[var(--color-success)]">
            <CircleCheck size={13} className="shrink-0" />
            created{proposal.decided_by ? ` by ${userName(proposal.decided_by)}` : ""}
          </span>
          {created && (
            <Button variant="accent" size="sm" asChild>
              <Link to={`/cases/${caseId}/stories/${created.id}`}>Open story</Link>
            </Button>
          )}
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
    </div>
  );
}
