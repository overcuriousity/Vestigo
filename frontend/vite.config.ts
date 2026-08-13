import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { readFileSync } from "fs";
import path from "path";

const pkg = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "package.json"), "utf-8"));

export default defineConfig({
  // Compile-time inject the package version so the footer never drifts from
  // package.json (see src/components/layout/Footer.tsx).
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    // Vitest stubs every CSS import to an empty string by default, `?raw`
    // included. `designSystem.test.ts` parses index.css for the set of defined
    // custom properties, so it needs the real text — scoped to that one file so
    // no other test starts paying for the Tailwind pipeline.
    css: { include: [/index\.css/] },
  },
});
