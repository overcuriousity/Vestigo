/**
 * Theme preference store — persisted to localStorage.
 * Drives the `data-theme` attribute on <html> (see index.html for the
 * pre-render sync that avoids a flash of the wrong theme, and App.tsx
 * for the runtime sync on toggle).
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";

export type Theme = "light" | "dark";

interface ThemeState {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
}

export const useThemeStore = create<ThemeState>()(
  persist(
    (set) => ({
      theme: "light",
      setTheme: (theme) => set({ theme }),
      toggleTheme: () =>
        set((s) => ({ theme: s.theme === "light" ? "dark" : "light" })),
    }),
    {
      name: "tsig-theme",
      version: 1,
    },
  ),
);
