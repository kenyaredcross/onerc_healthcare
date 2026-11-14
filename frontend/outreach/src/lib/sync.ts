/**
 * Sync engine (protocol §4; OUT-14..17, OUT-24, NFR-11).
 *
 * - Runs on app start/unlock, the `online` event, after a record is finished, and on "Sync now".
 * - Sends unacknowledged records in dependency order, in batches of `max_batch`, each with a batch_id.
 *   A batch whose answer never arrived is re-sent first with the same batch_id.
 * - Stops cleanly on a network error; the next run sends only what is still unacknowledged.
 * - Keeps every record until a final result; failed records are re-sent, held/conflict records are
 *   polled with sync.status until resolved. Final records are purged `purge_after_days` later.
 * - On 401 the records are kept and an online sign-in is requested. A Closed session still syncs with
 *   its grant (fix round 1, F1); a refusal that names the session status is remembered (sessionStatus).
 * - A failed record is re-sent unchanged at every run (the server retries it) until the user corrects
 *   it (F2, same client_uuid); records that depend on it wait and are sent after it is accepted.
 * - An accepted patient.update carries the patient's new `modified`: it becomes the local base (F4).
 */
import { ApiError, NetworkError, callMethod, outreachMethod, type DeviceCredentials } from "./api";
import type { Keys } from "./crypto";
import { getMeta, setMeta, delMeta, type OutboxRow } from "./db";
import { allRows, applyResult, getRow, markAttempt, orderForSend, purge, readSecret, waitingRows } from "./outbox";
import { getPatient, putPatient } from "./patients";
import { noteRefusal, noteSyncAccepted } from "./sessionStatus";
import {
  RECORD_PATIENT_REGISTER,
  RECORD_PATIENT_UPDATE,
  newUuid,
  type AnyEnvelope,
  type DeviceSettings,
  type PushArgs,
  type PushResponse,
  type RecordResult,
  type StatusResponse,
} from "./protocol";

export type SyncTrigger = "start" | "online" | "record" | "manual" | "timer";
export type StopReason = null | "locked" | "network" | "auth" | "server";

export interface SyncReport {
  trigger: SyncTrigger;
  sent: number;
  results: Record<string, number>;
  resolved: number;
  purged: number;
  stoppedBy: StopReason;
  error: string | null;
  finished_at: number;
}

export interface SyncState {
  last_run_at: number | null;
  last_success_at: number | null;
  last_error: string | null;
  stopped_by: StopReason;
  last_report: SyncReport | null;
}

export interface SyncContext {
  keys: Keys;
  session: string;
  deviceId: string;
  settings: DeviceSettings;
  credentials: DeviceCredentials;
  now?: () => number;
  onAuthRequired?: () => Promise<void> | void;
}

interface Inflight {
  batch_id: string;
  uuids: string[];
}

const STATUS_CHUNK = 200;
const listeners = new Set<() => void>();
let running: Promise<SyncReport> | null = null;

export function onSyncChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
export function notifySync(): void {
  listeners.forEach((fn) => fn());
}

export async function getSyncState(): Promise<SyncState> {
  return (
    (await getMeta<SyncState>("sync_state")) ?? {
      last_run_at: null,
      last_success_at: null,
      last_error: null,
      stopped_by: null,
      last_report: null,
    }
  );
}

export function isSyncRunning(): boolean {
  return running !== null;
}

/** Run one sync; concurrent calls share the run in progress. */
export function runSync(ctx: SyncContext, trigger: SyncTrigger): Promise<SyncReport> {
  if (!running) {
    running = doSync(ctx, trigger).finally(() => {
      running = null;
      notifySync();
    });
    notifySync();
  }
  return running;
}

async function afterFinal(keys: Keys, row: OutboxRow, result: RecordResult): Promise<void> {
  const secret = await readSecret(keys, row);
  if (!secret.patient_key) return;
  const patient = await getPatient(keys, secret.patient_key);
  if (!patient) return;
  if (row.type === RECORD_PATIENT_REGISTER && result.name && result.doctype === "Patient" && !patient.name) {
    // A `modified` (if the server sends one) lets this device edit the new patient before a package update.
    await putPatient(keys, { ...patient, name: result.name, ...(result.modified ? { modified: result.modified } : {}) });
  } else if (row.type === RECORD_PATIENT_UPDATE) {
    // F4: the server's new version becomes the local base, so the next edit on this device chains. The
    // local values already hold this edit (they were applied when it was made).
    const modified = result.modified && !result.discarded ? result.modified : null;
    await putPatient(keys, {
      ...patient,
      pending_updates: Math.max(0, patient.pending_updates - 1),
      ...(modified ? { modified } : {}),
    });
  }
}

async function record(keys: Keys, result: RecordResult, now: number, fromStatus: boolean, report: SyncReport): Promise<OutboxRow | null> {
  const before = await getRow(result.client_uuid);
  const row = await applyResult(keys, result, now, fromStatus);
  if (!row || !before) return row;
  if (!before.final && row.final) await afterFinal(keys, row, result);
  if (row.status === "resolved" && before.status !== "resolved") report.resolved += 1;
  return row;
}

async function doSync(ctx: SyncContext, trigger: SyncTrigger): Promise<SyncReport> {
  const now = ctx.now ?? (() => Date.now());
  const report: SyncReport = {
    trigger,
    sent: 0,
    results: {},
    resolved: 0,
    purged: 0,
    stoppedBy: null,
    error: null,
    finished_at: 0,
  };
  const state = await getSyncState();
  state.last_run_at = now();

  report.purged += await purge(ctx.settings.purge_after_days, now());

  const stop = async (e: unknown): Promise<void> => {
    if (e instanceof NetworkError) {
      report.stoppedBy = "network";
      report.error = e.message;
    } else if (e instanceof ApiError && e.needsSignIn) {
      await noteRefusal(e);
      report.stoppedBy = "auth";
      report.error = "The server needs an online sign-in before it accepts this device again. Your records are kept.";
      await ctx.onAuthRequired?.();
    } else if (e instanceof ApiError) {
      await noteRefusal(e);
      report.stoppedBy = "server";
      report.error = e.message;
    } else {
      report.stoppedBy = "server";
      report.error = e instanceof Error ? e.message : String(e);
    }
  };

  // ---- push --------------------------------------------------------------------------------
  const maxBatch = Math.max(1, ctx.settings.max_batch || 50);
  const ordered = orderForSend(await allRows());
  const byUuid = new Map(ordered.map((r) => [r.client_uuid, r]));
  const blocked = new Set<string>(); // failed or skipped in this run: their dependants wait
  const inflight = await getMeta<Inflight>("inflight_batch");

  const batches: Inflight[] = [];
  const queued = new Set<string>();
  if (inflight) {
    const uuids = inflight.uuids.filter((u) => byUuid.has(u));
    if (uuids.length) {
      batches.push({ batch_id: inflight.batch_id, uuids });
      uuids.forEach((u) => queued.add(u));
    } else {
      await delMeta("inflight_batch");
    }
  }
  const rest = ordered.filter((r) => !queued.has(r.client_uuid));
  for (let i = 0; i < rest.length; i += maxBatch) {
    batches.push({ batch_id: "", uuids: rest.slice(i, i + maxBatch).map((r) => r.client_uuid) });
  }

  for (const planned of batches) {
    const uuids = planned.uuids.filter((u) => {
      const row = byUuid.get(u)!;
      const waitsOnBlocked = row.depends_on.some((d) => blocked.has(d));
      if (waitsOnBlocked) blocked.add(u);
      return !waitsOnBlocked;
    });
    if (!uuids.length) continue;
    const batch: Inflight = {
      batch_id: planned.batch_id && uuids.length === planned.uuids.length ? planned.batch_id : newUuid(),
      uuids,
    };
    const records: AnyEnvelope[] = [];
    for (const u of uuids) records.push((await readSecret(ctx.keys, byUuid.get(u)!)).envelope);
    await setMeta("inflight_batch", batch);
    await markAttempt(uuids, now());
    const args: PushArgs = { session: ctx.session, device_id: ctx.deviceId, batch_id: batch.batch_id, records };
    let res: PushResponse;
    try {
      res = await callMethod<PushResponse>(outreachMethod("sync.push"), {
        method: "POST",
        body: args,
        device: ctx.credentials,
      });
    } catch (e) {
      await stop(e);
      if (report.stoppedBy === "server") await delMeta("inflight_batch"); // refused as a whole, not received
      break;
    }
    report.sent += records.length;
    await noteSyncAccepted();
    const answered = new Set<string>();
    for (const result of res.results ?? []) {
      if (!byUuid.has(result.client_uuid)) continue;
      answered.add(result.client_uuid);
      report.results[result.result] = (report.results[result.result] ?? 0) + 1;
      const row = await record(ctx.keys, result, now(), false, report);
      if (row && (row.status === "failed" || row.status === "pending")) blocked.add(row.client_uuid);
    }
    for (const u of uuids) if (!answered.has(u)) blocked.add(u); // no answer: stays pending
    await delMeta("inflight_batch");
    notifySync();
  }

  // ---- held / conflict: ask whether they were resolved --------------------------------------
  if (!report.stoppedBy) {
    const waiting = waitingRows(await allRows()).map((r) => r.client_uuid);
    for (let i = 0; i < waiting.length; i += STATUS_CHUNK) {
      const chunk = waiting.slice(i, i + STATUS_CHUNK);
      try {
        const res = await callMethod<StatusResponse>(outreachMethod("sync.status"), {
          params: { session: ctx.session, uuids: chunk },
          device: ctx.credentials,
        });
        await noteSyncAccepted();
        for (const result of res.results ?? []) {
          if (chunk.includes(result.client_uuid)) await record(ctx.keys, result, now(), true, report);
        }
      } catch (e) {
        await stop(e);
        break;
      }
    }
  }

  report.purged += await purge(ctx.settings.purge_after_days, now());
  report.finished_at = now();
  state.last_report = report;
  state.stopped_by = report.stoppedBy;
  state.last_error = report.error;
  if (!report.stoppedBy) state.last_success_at = report.finished_at;
  await setMeta("sync_state", state);
  return report;
}
