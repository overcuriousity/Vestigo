/**
 * Auth state — caches the current user for instant boot (avoids a flash of
 * the login screen while /auth/me resolves). The server session cookie is
 * always the source of truth; this store is a client-side mirror kept in
 * sync by `useCurrentUser` (TanStack Query) and cleared on logout/401.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { User } from "@/api/types";

interface AuthState {
  user: User | null;
  /** True once the initial /auth/me check has completed (success or 401). */
  initialized: boolean;
  setUser: (user: User | null) => void;
  setInitialized: (initialized: boolean) => void;
  clear: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      user: null,
      initialized: false,
      setUser: (user) => set({ user }),
      setInitialized: (initialized) => set({ initialized }),
      clear: () => set({ user: null }),
    }),
    {
      name: "tsig-auth",
      version: 1,
      // Never persist `initialized` — every fresh page load must re-validate
      // against the server session before trusting the cached user.
      partialize: (state) => ({ user: state.user }),
    },
  ),
);
