/**
 * Agent panel state — open/closed, width, and the active conversation per
 * timeline. Filters stay URL-owned (see ExplorerPage); this store never
 * holds filter state, only chat-panel bookkeeping.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";

interface AgentState {
  panelOpen: boolean;
  setPanelOpen: (open: boolean) => void;

  panelWidth: number;
  setPanelWidth: (w: number) => void;

  /** Active conversation id, keyed by "caseId/timelineId". */
  activeConversationByTimeline: Record<string, string | null>;
  setActiveConversation: (key: string, id: string | null) => void;
}

export const useAgentStore = create<AgentState>()(
  persist(
    (set) => ({
      panelOpen: false,
      setPanelOpen: (open) => set({ panelOpen: open }),

      panelWidth: 400,
      setPanelWidth: (w) => set({ panelWidth: w }),

      activeConversationByTimeline: {},
      setActiveConversation: (key, id) =>
        set((s) => ({
          activeConversationByTimeline: { ...s.activeConversationByTimeline, [key]: id },
        })),
    }),
    { name: "vestigo-agent" },
  ),
);
