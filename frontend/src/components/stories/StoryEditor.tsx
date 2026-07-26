import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { storiesApi } from "@/api/stories";
import type { StoryBlock, StoryBlockKind } from "@/api/types";
import { Spinner } from "@/components/ui/Spinner";
import { BlockFrame } from "./BlockFrame";
import { BlockPicker } from "./BlockPicker";
import { ChartBlockCard, EventBlockCard, ViewBlockCard } from "./EmbedCards";
import { MarkdownBlock } from "./MarkdownBlock";
import { afterIdForIndex, sortBlocks } from "./blockOrder";

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

  const moveBlock = useMutation({
    mutationFn: (vars: { blockId: string; afterBlockId: string | null; version: number }) =>
      storiesApi.moveBlock(caseId, storyId, vars.blockId, vars.afterBlockId, vars.version),
    // Both outcomes reconcile against the server: a 409 means a collaborator
    // moved it first, and their order is the one that stands.
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

  const insertAfter = (
    afterBlockId: string | null,
    kind: StoryBlockKind,
    content: Record<string, unknown>,
  ) => createBlock.mutate({ kind, content, afterBlockId });

  return (
    <div className="space-y-2">
      <Inserter
        caseId={caseId}
        onInsert={(kind, content) => insertAfter(null, kind, content)}
        label="Add at top"
      />
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
        <SortableContext items={blocks.map((b) => b.id)} strategy={verticalListSortingStrategy}>
          {blocks.map((block) => (
            <div key={block.id} className="space-y-2">
              <SortableBlock block={block} draggable={!editingIds.has(block.id)} onDelete={() => deleteBlock.mutate(block.id)}>
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
