/**
 * The outbox (NFR-11, OUT-16, OUT-24): every finished record stays here until the server gives it a
 * final result (accepted, duplicate, or a resolved conflict), and is purged `purge_after_days` after
 * that acknowledgement. Failed, held and conflict records are never purged.
 */
import { decryptJson, encryptJson, type Keys } from "./crypto";
import { aad, db, delMeta, getMeta, setMeta, type AckRow, type OutboxRow, type OutboxStatus } from "./db";
import type { AnyEnvelope, RecordResult } from "./protocol";

export const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * One line of a record's local history (fix round 1, F2): every distinct server error, and every
 * correction made on the device. Kept encrypted with the record; never sent.
 */
export interface HistoryEntry {
  kind: "failed" | "corrected";
  at: number; // epoch ms (first time, for a repeated error)
  last_at?: number; // epoch ms of the last identical answer
  count?: number; // how many identical answers in a row
  error?: string | null;
}

export interface OutboxSecret {
  envelope: AnyEnvelope;
  summary: string; // what the user sees in the sync screen, e.g. "Visit: Amina Hassan (Outpatient)"
  patient_key: string | null;
  history?: HistoryEntry[];
}

export interface OutboxItem {
  row: OutboxRow;
  secret: OutboxSecret;
  ack: RecordResult | null;
}

const SENDABLE: OutboxStatus[] = ["pending", "failed"];
const WAITING: OutboxStatus[] = ["held", "conflict"];

async function nextSeq(): Promise<number> {
  const seq = ((await getMeta<number>("outbox_seq")) ?? 0) + 1;
  await setMeta("outbox_seq", seq);
  return seq;
}

export async function addRecord(
  keys: Keys,
  envelope: AnyEnvelope,
  summary: string,
  patientKey: string | null = null,
  now: number = Date.now(),
): Promise<OutboxRow> {
  const secret: OutboxSecret = { envelope, summary, patient_key: patientKey };
  const row: OutboxRow = {
    client_uuid: envelope.client_uuid,
    seq: await nextSeq(),
    type: envelope.type,
    status: "pending",
    depends_on: [...envelope.depends_on],
    final: 0,
    created_at: now,
    last_attempt_at: null,
    acked_at: null,
    attempts: 0,
    enc: await encryptJson(keys, secret, aad("outbox", envelope.client_uuid)),
  };
  await (await db()).put("outbox", row);
  return row;
}

export async function allRows(): Promise<OutboxRow[]> {
  const rows = await (await db()).getAll("outbox");
  return rows.sort((a, b) => a.seq - b.seq);
}

export async function getRow(uuid: string): Promise<OutboxRow | undefined> {
  return (await db()).get("outbox", uuid);
}

export async function readSecret(keys: Keys, row: OutboxRow): Promise<OutboxSecret> {
  return decryptJson<OutboxSecret>(keys, row.enc, aad("outbox", row.client_uuid));
}

export async function readAck(keys: Keys, uuid: string): Promise<RecordResult | null> {
  const ack = await (await db()).get("acks", uuid);
  return ack ? decryptJson<RecordResult>(keys, ack.enc, aad("acks", uuid)) : null;
}

export async function listItems(keys: Keys): Promise<OutboxItem[]> {
  const out: OutboxItem[] = [];
  for (const row of await allRows()) {
    out.push({ row, secret: await readSecret(keys, row), ack: await readAck(keys, row.client_uuid) });
  }
  return out;
}

export type OutboxCounts = Record<OutboxStatus, number> & { total: number; unsynced: number };

export async function counts(): Promise<OutboxCounts> {
  const c: OutboxCounts = {
    pending: 0,
    failed: 0,
    held: 0,
    conflict: 0,
    accepted: 0,
    duplicate: 0,
    resolved: 0,
    total: 0,
    unsynced: 0,
  };
  for (const row of await allRows()) {
    c[row.status] += 1;
    c.total += 1;
    if (!row.final) c.unsynced += 1;
  }
  return c;
}

/** Records to send, in dependency order (a record after everything in its `depends_on`), then by seq. */
export function orderForSend(rows: OutboxRow[]): OutboxRow[] {
  const sendable = rows.filter((r) => !r.final && SENDABLE.includes(r.status)).sort((a, b) => a.seq - b.seq);
  const inSet = new Map(sendable.map((r) => [r.client_uuid, r]));
  const done = new Set<string>();
  const visiting = new Set<string>();
  const out: OutboxRow[] = [];
  const visit = (r: OutboxRow): void => {
    if (done.has(r.client_uuid) || visiting.has(r.client_uuid)) return;
    visiting.add(r.client_uuid);
    for (const dep of r.depends_on) {
      const d = inSet.get(dep);
      if (d) visit(d);
    }
    visiting.delete(r.client_uuid);
    done.add(r.client_uuid);
    out.push(r);
  };
  sendable.forEach(visit);
  return out;
}

export function waitingRows(rows: OutboxRow[]): OutboxRow[] {
  return rows.filter((r) => !r.final && WAITING.includes(r.status));
}

/**
 * Record the server's answer for one record. `fromStatus`: the answer came from sync.status for a held
 * or conflict record, so an accepted answer means the conflict was resolved.
 */
export async function applyResult(
  keys: Keys,
  result: RecordResult,
  now: number = Date.now(),
  fromStatus = false,
): Promise<OutboxRow | null> {
  const d = await db();
  const row = await d.get("outbox", result.client_uuid);
  if (!row) return null;
  if (row.final) return row; // already acknowledged; keep the first acknowledgement time
  let status: OutboxStatus;
  switch (result.result) {
    case "accepted":
      status = fromStatus || WAITING.includes(row.status) ? "resolved" : "accepted";
      break;
    case "duplicate":
      status = "duplicate";
      break;
    case "held":
      status = "held";
      break;
    case "conflict":
      status = "conflict";
      break;
    default:
      status = "failed";
  }
  const final = status === "accepted" || status === "duplicate" || status === "resolved";
  let enc = row.enc;
  if (status === "failed") {
    const secret = await readSecret(keys, row);
    secret.history = addFailure(secret.history ?? [], result.error ?? null, now);
    enc = await encryptJson(keys, secret, aad("outbox", row.client_uuid));
  }
  const updated: OutboxRow = { ...row, status, final: final ? 1 : 0, acked_at: final ? now : null, enc };
  const ack: AckRow = {
    client_uuid: row.client_uuid,
    result: result.result,
    acked_at: now,
    enc: await encryptJson(keys, result, aad("acks", row.client_uuid)),
  };
  const tx = d.transaction(["outbox", "acks"], "readwrite");
  await tx.objectStore("outbox").put(updated);
  await tx.objectStore("acks").put(ack);
  await tx.done;
  return updated;
}

/** Append a server error to the history, or count it again when it repeats the last line. */
export function addFailure(history: HistoryEntry[], error: string | null, now: number): HistoryEntry[] {
  const last = history[history.length - 1];
  if (last && last.kind === "failed" && (last.error ?? null) === error) {
    return [...history.slice(0, -1), { ...last, last_at: now, count: (last.count ?? 1) + 1 }];
  }
  return [...history, { kind: "failed", at: now, error, count: 1 }];
}

export class NotCorrectableError extends Error {
  constructor(message = "Only a record the server refused (failed) can be corrected.") {
    super(message);
    this.name = "NotCorrectableError";
  }
}

/**
 * Correct a failed record (fix round 1, F2): store the new payload under the SAME client_uuid, with the
 * same type, session, device, captured_at and depends_on, and queue it again. The server re-applies a
 * Failed record with the payload it receives; nothing else changes on the device.
 */
export async function requeueFailed(
  keys: Keys,
  uuid: string,
  payload: AnyEnvelope["payload"],
  opts: { summary?: string; now?: number } = {},
): Promise<OutboxRow> {
  const now = opts.now ?? Date.now();
  const d = await db();
  const row = await d.get("outbox", uuid);
  if (!row) throw new NotCorrectableError("This record is no longer on the device.");
  if (row.final || row.status !== "failed") throw new NotCorrectableError();
  const secret = await readSecret(keys, row);
  const envelope = { ...secret.envelope, payload } as AnyEnvelope;
  const updatedSecret: OutboxSecret = {
    ...secret,
    envelope,
    summary: opts.summary ?? secret.summary,
    history: [...(secret.history ?? []), { kind: "corrected", at: now }],
  };
  const updated: OutboxRow = {
    ...row,
    status: "pending",
    enc: await encryptJson(keys, updatedSecret, aad("outbox", uuid)),
  };
  await d.put("outbox", updated);
  // A batch whose answer never arrived must not be re-sent under its old batch_id with the new payload.
  const inflight = await getMeta<{ batch_id: string; uuids: string[] }>("inflight_batch");
  if (inflight?.uuids.includes(uuid)) await delMeta("inflight_batch");
  return updated;
}

export async function markAttempt(uuids: string[], now: number = Date.now()): Promise<void> {
  const d = await db();
  const tx = d.transaction("outbox", "readwrite");
  for (const id of uuids) {
    const row = await tx.store.get(id);
    if (row) await tx.store.put({ ...row, attempts: row.attempts + 1, last_attempt_at: now });
  }
  await tx.done;
}

/** Delete records acknowledged as final at least `purgeAfterDays` ago (OUT-24). Returns how many. */
export async function purge(purgeAfterDays: number, now: number = Date.now()): Promise<number> {
  const d = await db();
  const limit = now - purgeAfterDays * DAY_MS;
  const rows = await d.getAll("outbox");
  const doomed = rows.filter((r) => r.final === 1 && r.acked_at !== null && r.acked_at <= limit);
  if (!doomed.length) return 0;
  const tx = d.transaction(["outbox", "acks"], "readwrite");
  for (const r of doomed) {
    await tx.objectStore("outbox").delete(r.client_uuid);
    await tx.objectStore("acks").delete(r.client_uuid);
  }
  await tx.done;
  return doomed.length;
}
