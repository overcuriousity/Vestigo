/**
 * Global toast store — the imperative feedback channel for actions whose
 * outcome isn't visible next to the button that triggered them.
 *
 * Deliberately a plain zustand store with module-level `toast.*` helpers so
 * non-React code (the QueryClient's global mutation/query error handlers in
 * `lib/queryClient.ts`) can emit toasts without a hook.
 */
import { create } from "zustand";

export type ToastKind = "success" | "error" | "info";

/** Optional inline action button (e.g. "Undo") — clicking it dismisses the toast. */
export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface ToastItem {
  id: number;
  kind: ToastKind;
  title: string;
  description?: string;
  action?: ToastAction;
}

interface ToastState {
  toasts: ToastItem[];
  push: (kind: ToastKind, title: string, description?: string, action?: ToastAction) => void;
  dismiss: (id: number) => void;
}

let nextId = 1;

export const useToastStore = create<ToastState>((set, get) => ({
  toasts: [],
  push: (kind, title, description, action) => {
    // Dedup: a burst of identical failures (e.g. several queries hitting the
    // same dead endpoint on one page) collapses into one visible toast.
    const dup = get().toasts.some(
      (t) => t.kind === kind && t.title === title && t.description === description,
    );
    if (dup) return;
    set((s) => ({
      // Cap the stack so a pathological error storm can't fill the screen.
      toasts: [...s.toasts.slice(-4), { id: nextId++, kind, title, description, action }],
    }));
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

/** Imperative emitters — usable outside React (query cache, event handlers). */
export const toast = {
  success: (title: string, description?: string, action?: ToastAction) =>
    useToastStore.getState().push("success", title, description, action),
  error: (title: string, description?: string, action?: ToastAction) =>
    useToastStore.getState().push("error", title, description, action),
  info: (title: string, description?: string, action?: ToastAction) =>
    useToastStore.getState().push("info", title, description, action),
};
