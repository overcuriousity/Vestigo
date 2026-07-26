import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { ApiError } from "@/api/client";
import { storiesApi } from "@/api/stories";
import type { StoryBlock, StoryBlockKind } from "@/api/types";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { BlockFrame } from "./BlockFrame";
import { MarkdownBlock } from "./MarkdownBlock";
import { sortBlocks } from "./blockOrder";

interface Props {
  caseId: string;
  storyId: string;
}

/** Poll interval for the collaborative view of a story (no WebSockets). */
const POLL_MS = 10_000;

export function StoryEditor({ caseId, storyId }: Props) {
  const qc = useQueryClient();
  // Blocks under active edit: their local draft state must survive polling.
  const [editingIds, setEditingIds] = useState<Set<string>>(new Set());
  const [conflicts, setConflicts] = useState<Record<string, StoryBlock>>({});

  const { data, isLoading, error } = useQuery({
    queryKey: ["story", caseId, storyId],
    queryFn: () => storiesApi.getWithBlocks(caseId, storyId),
    refetchInterval: POLL_MS,
  });

  const blocks = useMemo(() => sortBlocks(data?.blocks ?? []), [data?.blocks]);

  const invalidate = () => qc.invalidateQueries({ queryKey: ["story", caseId, storyId] });

  const updateBlock = useMutation({
    mutationFn: (vars: { blockId: string; content: Record<string, unknown>; version: number }) =>
      storiesApi.updateBlock(caseId, storyId, vars.blockId, vars.content, vars.version),
    onSuccess: (_block, vars) => {
      setConflicts((prev) => {
        const next = { ...prev };
        delete next[vars.blockId];
        return next;
      });
      invalidate();
    },
    onError: async (err, vars) => {
      if (err instanceof ApiError && err.status === 409) {
        // The 409 body is collapsed to a message by the shared client, so
        // refetch and present the winning block from fresh data instead.
        const fresh = await storiesApi.getWithBlocks(caseId, storyId);
        const winner = fresh.blocks.find((b) => b.id === vars.blockId);
        if (winner) setConflicts((prev) => ({ ...prev, [vars.blockId]: winner }));
        qc.setQueryData(["story", caseId, storyId], fresh);
      }
    },
  });

  const createBlock = useMutation({
    mutationFn: (vars: {
      kind: StoryBlockKind;
      content: Record<string, unknown>;
      afterBlockId: string | null;
    }) =>
      storiesApi.createBlock(caseId, storyId, {
        kind: vars.kind,
        content: vars.content,
        after_block_id: vars.afterBlockId,
      }),
    onSuccess: invalidate,
  });

  const deleteBlock = useMutation({
    mutationFn: (blockId: string) => storiesApi.deleteBlock(caseId, storyId, blockId),
    onSuccess: invalidate,
  });

  const setEditing = useCallback((blockId: string, editing: boolean) => {
    setEditingIds((prev) => {
      const next = new Set(prev);
      if (editing) next.add(blockId);
      else next.delete(blockId);
      return next;
    });
  }, []);

  const resolveConflict = (blockId: string, choice: "theirs" | "mine") => {
    setConflicts((prev) => {
      const next = { ...prev };
      delete next[blockId];
      return next;
    });
    if (choice === "theirs") invalidate();
  };

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner />
      </div>
    );
  }
  if (error) {
    return <p className="text-sm text-[var(--color-danger)]">{(error as Error).message}</p>;
  }

  const addMarkdownAfter = (afterBlockId: string | null) =>
    createBlock.mutate({ kind: "markdown", content: { text: "" }, afterBlockId });

  return (
    <div className="space-y-2">
      <Inserter onAddMarkdown={() => addMarkdownAfter(null)} label="Add at top" />
      {blocks.map((block) => (
        <div key={block.id} className="space-y-2">
          <BlockFrame block={block} onDelete={() => deleteBlock.mutate(block.id)}>
            {block.kind === "markdown" ? (
              <MarkdownBlock
                block={block}
                conflict={conflicts[block.id] ?? null}
                onEditingChange={(editing) => setEditing(block.id, editing)}
                onSave={(text, version) =>
                  updateBlock.mutate({ blockId: block.id, content: { text }, version })
                }
                onResolveConflict={(choice) => resolveConflict(block.id, choice)}
              />
            ) : (
              <EmbedBlockBody block={block} caseId={caseId} />
            )}
          </BlockFrame>
          <Inserter onAddMarkdown={() => addMarkdownAfter(block.id)} />
        </div>
      ))}
      {blocks.length === 0 && (
        <p className="py-8 text-center text-sm text-[var(--color-fg-muted)]">
          Empty story. Write a paragraph, or push a view, chart or event into it from the
          analysis pages.
        </p>
      )}
      {editingIds.size > 0 && (
        <p className="text-[10px] text-[var(--color-fg-muted)]">
          Editing — your draft is kept while collaborators' changes stream in.
        </p>
      )}
    </div>
  );
}

/** Between-block insertion affordance; embed pickers arrive with the cards. */
function Inserter({
  onAddMarkdown,
  label = "Add block",
}: {
  onAddMarkdown: () => void;
  label?: string;
}) {
  return (
    <div className="flex justify-center opacity-0 transition-opacity hover:opacity-100 focus-within:opacity-100">
      <Button variant="ghost" size="sm" onClick={onAddMarkdown} aria-label={label}>
        <Plus size={12} /> Text
      </Button>
    </div>
  );
}

/** Placeholder body for embed blocks until the cards land (Task 13). */
function EmbedBlockBody({ block, caseId }: { block: StoryBlock; caseId: string }) {
  void caseId;
  return (
    <p className="text-xs text-[var(--color-fg-muted)]">
      {block.kind} · {JSON.stringify(block.content)}
    </p>
  );
}
