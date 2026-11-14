/// <reference types="vitest/config" />
import { defineConfig, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import { fileURLToPath } from "node:url";

// Built assets are served by Frappe from apps/onerc_healthcare/onerc_healthcare/public/outreach
// at /assets/onerc_healthcare/outreach/. The page itself is /outreach (www/outreach.html renders the
// hashed entry from .vite/manifest.json) and the service worker is served at /outreach/sw.js.
const ASSET_BASE = "/assets/onerc_healthcare/outreach/";
const OUT_DIR = fileURLToPath(new URL("../../onerc_healthcare/public/outreach", import.meta.url));

// `yarn dev`: proxy the API to the local bench (see README.md). Override with BENCH_URL / BENCH_HOST.
const BENCH_URL = process.env.BENCH_URL ?? "http://127.0.0.1:8004";
const BENCH_HOST = process.env.BENCH_HOST ?? "kalobeyei.localhost";
const proxy: ProxyOptions = {
  target: BENCH_URL,
  changeOrigin: false,
  headers: { Host: BENCH_HOST },
};

export default defineConfig(({ command }) => ({
  base: command === "build" ? ASSET_BASE : "/",
  plugins: [
    react(),
    VitePWA({
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      injectRegister: false, // src/main.tsx registers /outreach/sw.js with scope /outreach/
      registerType: "autoUpdate",
      // The web manifest is public/manifest.webmanifest (absolute icon URLs; precached by the glob below).
      manifest: false,
      injectManifest: {
        globPatterns: ["**/*.{js,css,svg,png,webmanifest}"],
        globIgnores: ["sw.js", "**/node_modules/**"],
        // The SW lives at /outreach/sw.js, so precache URLs must be absolute.
        modifyURLPrefix: { "": ASSET_BASE },
        maximumFileSizeToCacheInBytes: 4 * 1024 * 1024,
      },
      devOptions: { enabled: false },
    }),
  ],
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
    manifest: true,
    sourcemap: false,
    target: "es2020",
  },
  server: {
    port: 5173,
    proxy: { "/api": proxy, "/assets": proxy, "/files": proxy },
  },
  test: {
    environment: "node",
    globals: true,
    setupFiles: ["tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
    testTimeout: 30000,
  },
}));
