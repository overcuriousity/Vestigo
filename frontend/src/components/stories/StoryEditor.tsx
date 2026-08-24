import { useCallback, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { ApiError } from "@/api/client";
import { storiesApi, type StoryWithBlocks } from "@/api/stories";
import type { StoryBlock, StoryBlockKind, StoryBlockOf } from "@/api/types";
import { Spinner } from "@/components/ui/Spinner";
import { toast } from "@/stores/toasts";
import { BlockFrame } from "./BlockFrame";
import { BlockPicker } from "./BlockPicker";
import { ChartBlockCard, EventBlockCard, ViewBlockCard } from "./EmbedCards";
import { MarkdownBlock } from "./MarkdownBlock";
import { storyQueryKey, useInvalidateStory, useStory } from "./useStory";
import { afterIdForIndex, reorderLocally, sortBlocks } from "./blockOrder";
import { nextEditingIds } from "./editingIds";

interface Props {
  caseId: string;
  storyId: string;
}

export function StoryEditor({ caseId, storyId }: Props) {
  const qc = useQueryClient();
  // Blocks under active edit: their local draft state must survive polling.
  const [editingIds, setEditingIds] = useState<Set<string>>(new Set());
  const [conflicts, setConflicts] = useState<Record<string, StoryBlockOf<"markdown">>>({});

  // The editor is the consumer that needs the collaborative poll.
  const { data, isLoading, error } = useStory(caseId, storyId, { poll: true });

  const blocks = useMemo(() => sortBlocks(data?.blocks ?? []), [data?.blocks]);

  const invalidate = useInvalidateStory(caseId, storyId);

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
        // Only markdown blocks have an inline conflict UI; an embed block's
        // content is a reference, not text a collaborator can diverge on.
        if (winner?.kind === "markdown") {
          setConflicts((prev) => ({ ...prev, [vars.blockId]: winner }));
        }
        qc.setQueryData(storyQueryKey(caseId, storyId), fresh);
      }
    },
  });

  const createBlock = useMutation({
    mutationFn: (vars: {
      kind: StoryBlockKind;
      content: Record<string, unknown>;
      afterBlockId: string | null;
      atTop?: boolean;
    }) =>
      storiesApi.createBlock(caseId, storyId, {
        kind: vars.kind,
        content: vars.content,
        // `after_block_id: null` appends on create (it only means "top" on
        // move), so the top inserter has to say so explicitly or its block
        // lands at the bottom.
        ...(vars.atTop ? { at_top: true } : { after_block_id: vars.afterBlockId }),
      }),
    onSuccess: invalidate,
    // Without this a rejected insert is completely silent: the picker closes
    // and no block appears.
    onError: (err) => toast.error("Could not add the block", (err as Error).message),
  });

  const deleteBlock = useMutation({
    mutationFn: (vars: { blockId: string; version: number }) =>
      storiesApi.deleteBlock(caseId, storyId, vars.blockId, vars.version),
    onSuccess: invalidate,
    onError: (err) => {
      // A 409 means the block was edited between this render and the click.
      // Refetching is the resolution: the user sees what changed and can
      // decide again, rather than being told the delete "failed".
      if (err instanceof ApiError && err.status === 409) {
        invalidate();
        toast.info(
          "A collaborator edited that block first",
          "Reloaded — delete it again if you still want it gone.",
        );
        return;
      }
      toast.error("Could not delete the block", (err as Error).message);
    },
  });

  const moveBlock = useMutation({
    mutationFn: (vars: { blockId: string; afterBlockId: string | null; version: number }) =>
      storiesApi.moveBlock(caseId, storyId, vars.blockId, vars.afterBlockId, vars.version),
    // Optimistic: the dragged block stays where it was dropped instead of
    // snapping back for the round-trip. `onSettled` reconciles against the
    // server either way.
    onMutate: async (vars) => {
      await qc.cancelQueries({ queryKey: storyQueryKey(caseId, storyId) });
      const previous = qc.getQueryData<StoryWithBlocks>(storyQueryKey(caseId, storyId));
      if (previous) {
        qc.setQueryData<StoryWithBlocks>(storyQueryKey(caseId, storyId), {
          ...previous,
          blocks: reorderLocally(sortBlocks(previous.blocks), vars.blockId, vars.afterBlockId),
        });
      }
      return { previous };
    },
    onError: (err, _vars, context) => {
      if (context?.previous) qc.setQueryData(storyQueryKey(caseId, storyId), context.previous);
      if (err instanceof ApiError && err.status === 409) {
        toast.info("A collaborator moved that block first", "Their order stands.");
      } else {
        toast.error("Could not move the block", (err as Error).message);
      }
    },
    onSettled: invalidate,
  });

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const onDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const targetIndex = blocks.findIndex((b) => b.id === over.id);
    const moving = blocks.find((b) => b.id === active.id);
    if (!moving || targetIndex < 0) return;
    moveBlock.mutate({
      blockId: moving.id,
      afterBlockId: afterIdForIndex(blocks, targetIndex, moving.id),
      version: moving.version,
    });
  };

  const setEditing = useCallback((blockId: string, editing: boolean) => {
    setEditingIds((prev) => nextEditingIds(prev, blockId, editing));
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

  const insertAfter = (
    afterBlockId: string | null,
    kind: StoryBlockKind,
    content: Record<string, unknown>,
    atTop = false,
  ) => createBlock.mutate({ kind, content, afterBlockId, atTop });

  return (
    <div className="space-y-2">
      <Inserter
        caseId={caseId}
        onInsert={(kind, content) => insertAfter(null, kind, content, true)}
        label="Add at top"
      />
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
        <SortableContext items={blocks.map((b) => b.id)} strategy={verticalListSortingStrategy}>
          {blocks.map((block) => (
            <div key={block.id} className="space-y-2">
              <SortableBlock block={block} draggable={!editingIds.has(block.id)} onDelete={() => deleteBlock.mutate({ blockId: block.id, version: block.version })}>
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
                ) : block.kind === "view_ref" ? (
                  <ViewBlockCard block={block} caseId={caseId} />
                ) : block.kind === "chart_ref" ? (
                  <ChartBlockCard block={block} caseId={caseId} />
                ) : (
                  <EventBlockCard block={block} caseId={caseId} />
                )}
              </SortableBlock>
              <Inserter
                caseId={caseId}
                onInsert={(kind, content) => insertAfter(block.id, kind, content)}
              />
            </div>
          ))}
        </SortableContext>
      </DndContext>
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

/** Between-block insertion affordance: text inline, embeds via the picker. */
function Inserter({
  caseId,
  onInsert,
  label,
}: {
  caseId: string;
  onInsert: (kind: StoryBlockKind, content: Record<string, unknown>) => void;
  label?: string;
}) {
  return (
    <div className="flex justify-center opacity-0 transition-opacity hover:opacity-100 focus-within:opacity-100">
      <BlockPicker caseId={caseId} onInsert={onInsert} label={label} />
    </div>
  );
}

/**
 * One draggable block. Dragging is disabled while its markdown is being
 * edited — a grab gesture inside a textarea is a text selection, not a move.
 */
function SortableBlock({
  block,
  children,
  onDelete,
  draggable,
}: {
  block: StoryBlock;
  children: React.ReactNode;
  onDelete: () => void;
  draggable: boolean;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: block.id,
    disabled: !draggable,
  });

  return (
    <BlockFrame
      block={block}
      onDelete={onDelete}
      containerRef={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      dragging={isDragging}
      handleProps={{ ...attributes, ...listeners }}
    >
      {children}
    </BlockFrame>
  );
}
