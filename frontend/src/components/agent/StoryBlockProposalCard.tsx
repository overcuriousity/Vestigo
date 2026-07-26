/**
 * StoryBlockProposalCard — renders an agent `propose_story_block` call as a
 * confirm/reject-able card.
 *
 * Same sandbox + apply model as the annotation and chart cards: the agent
 * drafts, the analyst's click is the write. Confirming creates the block with
 * `origin: agent`; a chart proposed inline is saved as a chart at the same
 * moment, so the block references a persisted object like every other embed.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { BookOpenText, CircleCheck, CircleX } from "lucide-react";
import { agentApi, type AgentProposal } from "@/api/agent";
import { ApiError } from "@/api/client";
import { storiesApi } from "@/api/stories";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { useUserNames } from "@/hooks/useUserNames";
import { toast } from "@/stores/toasts";
import { Markdown } from "./Markdown";

interface Props {
  caseId: string;
  conversationId: string;
  proposal: AgentProposal;
}

const KIND_LABEL: Record<string, string> = {
  markdown: "text",
  view_ref: "saved view",
  chart_ref: "chart",
  event_ref: "event",
};

export function StoryBlockProposalCard({ caseId, conversationId, proposal }: Props) {
  const queryClient = useQueryClient();
  const queryKey = ["agent-proposals", caseId, conversationId];
  const payload = proposal.payload;
  const userName = useUserNames();

  // The target story may have been renamed or deleted since the proposal —
  // name it if it still exists, say so plainly if it doesn't.
  const { data: stories } = useQuery({
    queryKey: ["stories", caseId],
    queryFn: () => storiesApi.list(caseId),
  });
  const story = stories?.find((s) => s.id === payload?.story_id);

  const onDecideError = (err: Error) => {
    // A 409 means another tab/analyst decided first — refetch and render the
    // decided state rather than surfacing an error.
    if (err instanceof ApiError && err.status === 409) return;
    toast.error("Proposal update failed", err.message);
  };

  const confirmMutation = useMutation({
    mutationFn: () => agentApi.confirmProposal(caseId, conversationId, proposal.id),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["story", caseId, payload?.story_id] });
      if (res.applied === false && res.reason) {
        toast.error("Nothing was added", res.reason);
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

  if (proposal.status === "rejected") {
    return (
      <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg-surface)] p-2 text-xs text-[var(--color-fg-secondary)]">
        <div className="flex items-center gap-1.5">
          <CircleX size={13} className="shrink-0" />
          <span>
            Story block proposal rejected
            {proposal.decided_by ? ` by ${userName(proposal.decided_by)}` : ""}
          </span>
        </div>
      </div>
    );
  }

  const blockKind = payload?.block_kind ?? "markdown";
  const content = (payload?.content ?? {}) as Record<string, unknown>;

  return (
    <div className="rounded-md border border-[var(--color-accent)] bg-[var(--color-accent-dim)] p-2.5 text-xs">
      <div className="flex items-center gap-1.5 font-semibold text-[var(--color-fg-primary)]">
        <BookOpenText size={13} className="shrink-0 text-[var(--color-accent)]" />
        <span className="min-w-0 break-words">
          Story block proposal · {KIND_LABEL[blockKind] ?? blockKind}
        </span>
      </div>
      <p className="mt-1 text-[var(--color-fg-secondary)]">
        {story ? (
          <>
            for{" "}
            <Link
              to={`/cases/${caseId}/stories/${story.id}`}
              className="text-[var(--color-accent)] hover:underline"
            >
              {story.title}
            </Link>
          </>
        ) : (
          "for a story that no longer exists"
        )}
      </p>

      <div className="mt-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)] p-2">
        {blockKind === "markdown" ? (
          <Markdown content={(content.text as string) ?? ""} />
        ) : blockKind === "chart_ref" && content.chart_spec ? (
          <p className="text-[var(--color-fg-secondary)]">
            New chart “{(content.name as string) ?? "untitled"}” — saved to the timeline when
            you confirm.
          </p>
        ) : (
          <p className="font-mono text-[10px] text-[var(--color-fg-secondary)]">
            {JSON.stringify(content)}
          </p>
        )}
      </div>

      {proposal.rationale && (
        <p className="mt-1.5 text-[var(--color-fg-secondary)]">{proposal.rationale}</p>
      )}

      {proposal.status === "confirmed" ? (
        <div className="mt-2 flex items-center justify-between gap-2">
          <span className="flex items-center gap-1 text-[var(--color-success)]">
            <CircleCheck size={13} className="shrink-0" />
            added{proposal.decided_by ? ` by ${userName(proposal.decided_by)}` : ""}
          </span>
          {story && (
            <Button variant="accent" size="sm" asChild>
              <Link to={`/cases/${caseId}/stories/${story.id}`}>Open story</Link>
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
