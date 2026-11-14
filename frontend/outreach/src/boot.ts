/**
 * Boot info rendered by www/outreach.py into the page (absent under `yarn dev`). The page is cached by the
 * service worker for offline use, so it carries no CSRF token and no user (fix round 1, F7): the app gets
 * those from `get_boot` (`fetchBoot`) when it is online.
 */
import type { BootInfo } from "./lib/api";

export type PageBoot = Pick<BootInfo, "site_name" | "timezone" | "sw_url" | "build">;

declare global {
  interface Window {
    __OUTREACH_BOOT__?: PageBoot;
  }
}

export const BASENAME = "/outreach";

export function pageBoot(): PageBoot | null {
  return typeof window !== "undefined" ? (window.__OUTREACH_BOOT__ ?? null) : null;
}
