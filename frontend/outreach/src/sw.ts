/// <reference lib="webworker" />
/**
 * Service worker, served by Frappe at /outreach/sw.js with scope /outreach/ (www/outreach.py).
 *
 * - Precaches the built bundle (hashed JS/CSS, icons, manifest) from /assets/onerc_healthcare/outreach/.
 * - Navigations under /outreach/ go to the network first and fall back to the cached app shell, so the
 *   app opens offline. The shell is the /outreach page itself (it carries the boot info).
 * - /api/ responses are never cached: they carry patient data and must stay in encrypted IndexedDB.
 */
import { cleanupOutdatedCaches, precacheAndRoute } from "workbox-precaching";
import { clientsClaim } from "workbox-core";

declare const self: ServiceWorkerGlobalScope & { __WB_MANIFEST: Array<{ url: string; revision: string | null }> };

const SHELL_CACHE = "krcs-outreach-shell-v1";
const SHELL_URL = "/outreach/";
const NAV_TIMEOUT_MS = 5000;

self.skipWaiting();
clientsClaim();
cleanupOutdatedCaches();
precacheAndRoute(self.__WB_MANIFEST);

function isApi(url: URL): boolean {
  return url.pathname.startsWith("/api/");
}

function isShellResponse(res: Response): boolean {
  return res.ok && !res.redirected && (res.headers.get("content-type") ?? "").includes("text/html");
}

async function refreshShell(): Promise<void> {
  try {
    const res = await fetch(SHELL_URL, { credentials: "same-origin", cache: "no-store" });
    if (isShellResponse(res)) await (await caches.open(SHELL_CACHE)).put(SHELL_URL, res);
  } catch {
    /* offline: keep the cached shell */
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(refreshShell());
});

async function handleNavigation(request: Request): Promise<Response> {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), NAV_TIMEOUT_MS);
    const res = await fetch(request, { signal: controller.signal });
    clearTimeout(timer);
    if (isShellResponse(res)) {
      await cache.put(SHELL_URL, res.clone());
      return res;
    }
    if (res.ok || res.status < 500) return res;
  } catch {
    /* offline or slow: fall back to the shell */
  }
  const cached = await cache.match(SHELL_URL);
  return (
    cached ??
    new Response("<h1>KRCS Outreach is offline</h1><p>Open the app once while online to install it.</p>", {
      status: 503,
      headers: { "Content-Type": "text/html; charset=utf-8" },
    })
  );
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || isApi(url) || event.request.method !== "GET") return;
  const inScope = url.pathname === "/outreach" || url.pathname.startsWith("/outreach/");
  if (event.request.mode === "navigate" && inScope && url.pathname !== "/outreach/sw.js") {
    event.respondWith(handleNavigation(event.request));
  }
});

self.addEventListener("message", (event) => {
  if (event.data === "refresh-shell") event.waitUntil(refreshShell());
});
