/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: run the backend (`prr serve … --port 8765`), then `npm run dev` and open the
// printed /?t=<token> URL on the Vite port instead. The proxy forwards API calls and the
// token handshake, and rewrites Origin so the backend's same-origin check passes.
const target = `http://127.0.0.1:${process.env.PRR_PORT || 8765}`;
const forward = { target, changeOrigin: true, headers: { origin: target } };

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 2000 },
  server: {
    proxy: {
      "/api": forward,
      "^/\\?t=": forward,
    },
  },
  test: { environment: "jsdom", setupFiles: ["src/test/setup.ts"], include: ["src/**/*.test.{ts,tsx}"] },
});
