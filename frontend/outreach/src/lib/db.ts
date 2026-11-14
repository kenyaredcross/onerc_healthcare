/**
 * IndexedDB layout (protocol §5). Every value that holds patient, clinical or credential data is
 * AES-GCM encrypted under the data key (`enc`); the only plain fields are what the app must read while
 * locked or use as an index: random UUIDs, record type/status, timestamps and HMAC search tokens.
 *
 * Stores:
 * - `meta`      key/value: device_id, the PIN-wrapped key, PIN attempts, expiry, grant name (plain);
 *               `secure:*` keys hold encrypted values (sync secret, user, settings, session).
 * - `reference` the package reference data (one encrypted blob + its version hash).
 * - `patients`  one encrypted row per patient; `tokens` (multiEntry index) are HMAC search tokens.
 * - `outbox`    finished records waiting for (or kept after) the server's answer.
 * - `acks`      the server's answer for each record (encrypted), used for display and purge.
 * - `drafts`    visits still being captured (encrypted), kept until finished or abandoned.
 */
import { openDB, type DBSchema, type IDBPDatabase } from "idb";
import { decryptJson, encryptJson, type Encrypted, type Keys } from "./crypto";
import type { RecordType } from "./protocol";

export const DB_NAME = "krcs-outreach";
export const DB_VERSION = 1;

export type OutboxStatus = "pending" | "failed" | "held" | "conflict" | "accepted" | "duplicate" | "resolved";

export interface OutboxRow {
  client_uuid: string;
  seq: number;
  type: RecordType;
  status: OutboxStatus;
  depends_on: string[];
  final: 0 | 1;
  created_at: number; // epoch ms
  last_attempt_at: number | null;
  acked_at: number | null; // epoch ms of the final acknowledgement
  attempts: number;
  enc: Encrypted; // OutboxSecret
}

export interface AckRow {
  client_uuid: string;
  result: string;
  acked_at: number;
  enc: Encrypted; // RecordResult
}

export interface PatientRow {
  key: string;
  tokens: string[];
  local: 0 | 1; // registered on this device, not (yet) from a package
  enc: Encrypted; // LocalPatient
}

export interface DraftRow {
  id: string;
  updated_at: number;
  enc: Encrypted;
}

export interface ReferenceRow {
  version: string;
  enc: Encrypted;
}

interface OutreachDB extends DBSchema {
  meta: { key: string; value: unknown };
  reference: { key: string; value: ReferenceRow };
  patients: { key: string; value: PatientRow; indexes: { tokens: string; local: number } };
  outbox: { key: string; value: OutboxRow; indexes: { status: string; seq: number } };
  acks: { key: string; value: AckRow };
  drafts: { key: string; value: DraftRow };
}

export type DB = IDBPDatabase<OutreachDB>;

let dbPromise: Promise<DB> | null = null;

export function db(): Promise<DB> {
  if (!dbPromise) {
    dbPromise = openDB<OutreachDB>(DB_NAME, DB_VERSION, {
      upgrade(database) {
        database.createObjectStore("meta");
        database.createObjectStore("reference");
        const patients = database.createObjectStore("patients", { keyPath: "key" });
        patients.createIndex("tokens", "tokens", { multiEntry: true });
        patients.createIndex("local", "local");
        const outbox = database.createObjectStore("outbox", { keyPath: "client_uuid" });
        outbox.createIndex("status", "status");
        outbox.createIndex("seq", "seq");
        database.createObjectStore("acks", { keyPath: "client_uuid" });
        database.createObjectStore("drafts", { keyPath: "id" });
      },
    });
  }
  return dbPromise;
}

/** Close the connection (tests swap the IndexedDB factory between cases). */
export async function closeDb(): Promise<void> {
  if (dbPromise) {
    const d = await dbPromise;
    d.close();
    dbPromise = null;
  }
}

// ----- meta ----------------------------------------------------------------------------------

export async function getMeta<T>(key: string): Promise<T | undefined> {
  return (await (await db()).get("meta", key)) as T | undefined;
}

export async function setMeta(key: string, value: unknown): Promise<void> {
  await (await db()).put("meta", value, key);
}

export async function delMeta(key: string): Promise<void> {
  await (await db()).delete("meta", key);
}

export async function getSecure<T>(keys: Keys, key: string): Promise<T | undefined> {
  const value = (await (await db()).get("meta", `secure:${key}`)) as Encrypted | undefined;
  return value ? decryptJson<T>(keys, value, `meta:${key}`) : undefined;
}

export async function setSecure(keys: Keys, key: string, value: unknown): Promise<void> {
  await setMeta(`secure:${key}`, await encryptJson(keys, value, `meta:${key}`));
}

// ----- reference ------------------------------------------------------------------------------

export async function getReferenceVersion(): Promise<string | null> {
  return (await (await db()).get("reference", "reference"))?.version ?? null;
}

export async function getReference<T>(keys: Keys): Promise<T | null> {
  const row = await (await db()).get("reference", "reference");
  return row ? decryptJson<T>(keys, row.enc, `reference:${row.version}`) : null;
}

export async function putReference(keys: Keys, version: string, data: unknown): Promise<void> {
  const enc = await encryptJson(keys, data, `reference:${version}`);
  await (await db()).put("reference", { version, enc }, "reference");
}

// ----- generic encrypted rows -----------------------------------------------------------------

export const aad = (store: string, key: string): string => `${store}:${key}`;

/** Remove everything on the device (used only when switching to another session with nothing pending). */
export async function wipeAll(keepMeta: string[] = ["device_id"]): Promise<void> {
  const d = await db();
  const kept: Record<string, unknown> = {};
  for (const k of keepMeta) kept[k] = await d.get("meta", k);
  const tx = d.transaction(["meta", "reference", "patients", "outbox", "acks", "drafts"], "readwrite");
  await Promise.all([
    tx.objectStore("meta").clear(),
    tx.objectStore("reference").clear(),
    tx.objectStore("patients").clear(),
    tx.objectStore("outbox").clear(),
    tx.objectStore("acks").clear(),
    tx.objectStore("drafts").clear(),
  ]);
  for (const [k, v] of Object.entries(kept)) if (v !== undefined) await tx.objectStore("meta").put(v, k);
  await tx.done;
}
