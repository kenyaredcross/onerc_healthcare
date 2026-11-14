/**
 * What the device knows about its outreach session's status on the server (fix round 1, F1).
 *
 * - **Closed**: no new work (no package, no enrolment of a new device), but the device grant still syncs the
 *   records captured during the session, and `enrol` renews this device's grant.
 * - **Cancelled**: the server refuses everything. Records still on the device are kept (never deleted); the
 *   HIS Officer must be contacted.
 * - **Ended**: the session is no longer listed by `devices.my_sessions` (Planned, Active, Returned) but the
 *   device does not know whether it was closed or cancelled. A later successful sync call means Closed.
 *
 * Sources, all best-effort: `my_sessions` after an online sign-in, and the message of a refused call
 * (the server's refusal names the status, e.g. "Outreach session X is Cancelled; ...").
 * Kept in IndexedDB `meta` (plain: a status word and a time, no patient data).
 */
import { ApiError, callMethod, outreachMethod } from "./api";
import { getMeta, setMeta } from "./db";
import type { OutreachSessionSummary } from "./protocol";

export type EndedStatus = "Closed" | "Cancelled" | "Ended";

export interface SessionStatusInfo {
  status: string; // Planned / Active / Returned, or an EndedStatus
  checked_at: number;
  message: string | null;
}

const KEY = "session_status";
const listeners = new Set<() => void>();

export function onSessionStatusChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function isEnded(status: string | null | undefined): status is EndedStatus {
  return status === "Closed" || status === "Cancelled" || status === "Ended";
}

export async function getSessionStatus(): Promise<SessionStatusInfo | null> {
  return (await getMeta<SessionStatusInfo>(KEY)) ?? null;
}

export async function setSessionStatus(status: string, message: string | null = null, now: number = Date.now()): Promise<void> {
  const current = await getSessionStatus();
  // Cancelled is the strongest knowledge; only a positive listing (the session is open again) replaces it.
  if (current?.status === "Cancelled" && (status === "Closed" || status === "Ended")) return;
  // Closed is more precise than Ended.
  if (current?.status === "Closed" && status === "Ended") return;
  if (current && current.status === status && current.message === message) return;
  await setMeta(KEY, { status, checked_at: now, message } satisfies SessionStatusInfo);
  listeners.forEach((fn) => fn());
}

/** The ended status named by a refused call, or null. Only whole-call errors, never a record's error. */
export function endedStatusFromError(e: unknown): "Closed" | "Cancelled" | null {
  if (!(e instanceof ApiError)) return null;
  const msg = e.message;
  if (!/session/i.test(msg)) return null;
  if (/\bcancell?ed\b/i.test(msg)) return "Cancelled";
  if (/\bclosed\b/i.test(msg)) return "Closed";
  return null;
}

/** Remember the status a refused call revealed. Returns it (or null). */
export async function noteRefusal(e: unknown): Promise<"Closed" | "Cancelled" | null> {
  const status = endedStatusFromError(e);
  if (status) await setSessionStatus(status, e instanceof Error ? e.message : null);
  return status;
}

/** A device call of the session succeeded: an "Ended" session that still accepts records is Closed. */
export async function noteSyncAccepted(): Promise<void> {
  const current = await getSessionStatus();
  if (current?.status === "Ended") await setSessionStatus("Closed", current.message);
}

/**
 * After an online sign-in: ask `devices.my_sessions` (Planned, Active, Returned). A session no longer
 * listed has ended (closed, cancelled, or the user left the team). Errors are ignored (offline).
 */
export async function refreshSessionStatus(session: string): Promise<SessionStatusInfo | null> {
  try {
    const sessions = await callMethod<OutreachSessionSummary[]>(outreachMethod("devices.my_sessions"));
    const mine = sessions.find((s) => s.name === session);
    if (mine) {
      const current = await getSessionStatus();
      if (!current || current.status !== mine.status) {
        await setMeta(KEY, { status: mine.status, checked_at: Date.now(), message: null } satisfies SessionStatusInfo);
        listeners.forEach((fn) => fn());
      }
    } else {
      await setSessionStatus("Ended", null);
    }
  } catch {
    /* offline or signed out: keep what is known */
  }
  return getSessionStatus();
}
