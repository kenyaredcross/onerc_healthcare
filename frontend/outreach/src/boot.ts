/** Boot info rendered by www/outreach.py into the page (absent under `yarn dev`). */
import type { BootInfo } from "./lib/api";

declare global {
  interface Window {
    __OUTREACH_BOOT__?: BootInfo;
  }
}

export const BASENAME = "/outreach";

export function pageBoot(): BootInfo | null {
  return typeof window !== "undefined" ? (window.__OUTREACH_BOOT__ ?? null) : null;
}
