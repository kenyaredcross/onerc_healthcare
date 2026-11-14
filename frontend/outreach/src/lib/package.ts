/**
 * Package download (protocol §3): reference data (skipped when the version is unchanged) and the
 * catchment patients, paged, with progress. Everything is stored encrypted.
 */
import { callMethod, outreachMethod, type DeviceCredentials } from "./api";
import type { Keys } from "./crypto";
import { db, getReferenceVersion, putReference, setMeta } from "./db";
import { fromPackage, getPatient, patientKey, putPatient, type LocalPatient } from "./patients";
import type { PatientsPage, ReferenceResponse } from "./protocol";
import { clearReferenceCache } from "./reference";

export interface DownloadProgress {
  phase: "reference" | "patients" | "done";
  loaded: number;
  total: number;
}

export interface DownloadOptions {
  keys: Keys;
  session: string;
  credentials: DeviceCredentials;
  pageSize?: number;
  onProgress?: (p: DownloadProgress) => void;
  signal?: AbortSignal;
}

export async function downloadReference(opts: DownloadOptions): Promise<{ version: string; changed: boolean }> {
  const current = await getReferenceVersion();
  const res = await callMethod<ReferenceResponse>(outreachMethod("package.get_reference"), {
    params: { session: opts.session, version: current ?? undefined },
    device: opts.credentials,
    signal: opts.signal,
  });
  if (res.unchanged) return { version: res.version, changed: false };
  await putReference(opts.keys, res.version, res.data);
  clearReferenceCache();
  return { version: res.version, changed: true };
}

export async function downloadPatients(opts: DownloadOptions): Promise<number> {
  const pageSize = opts.pageSize ?? 500;
  let page = 0;
  let loaded = 0;
  let total = 0;
  const seen = new Set<string>();
  // Patients registered here and since accepted by the server come back in the package under their
  // server name: the package row replaces the local one (keeping the visits captured here).
  const d = await db();
  const localByName = new Map<string, LocalPatient>();
  for (const k of (await d.getAllKeysFromIndex("patients", "local", 1)) as string[]) {
    const lp = await getPatient(opts.keys, k);
    if (lp?.name) localByName.set(lp.name, lp);
  }
  for (;;) {
    const res = await callMethod<PatientsPage>(outreachMethod("package.get_patients"), {
      params: { session: opts.session, page, page_size: pageSize },
      device: opts.credentials,
      signal: opts.signal,
    });
    total = res.total;
    for (const p of res.patients) {
      const key = await patientKey(opts.keys, { name: p.name, client_uuid: null });
      seen.add(key);
      const existing = await getPatient(opts.keys, key);
      const fresh = fromPackage(key, p);
      const registeredHere = localByName.get(p.name);
      if (registeredHere) {
        fresh.outreach_visits = registeredHere.outreach_visits;
        await d.delete("patients", registeredHere.key);
      }
      if (existing) {
        // Keep what was captured here: outreach visits, and local edits still waiting for sync.
        fresh.outreach_visits = existing.outreach_visits;
        if (existing.pending_updates > 0) {
          await putPatient(opts.keys, { ...existing, recent_visits: fresh.recent_visits, active_prescriptions: fresh.active_prescriptions });
          continue;
        }
      }
      await putPatient(opts.keys, fresh);
    }
    loaded += res.patients.length;
    opts.onProgress?.({ phase: "patients", loaded, total });
    if (!res.has_more || !res.patients.length) break;
    page += 1;
  }
  // Package patients no longer in the catchment are removed from the device; local ones stay.
  const serverKeys = (await d.getAllKeysFromIndex("patients", "local", 0)) as string[];
  for (const k of serverKeys) if (!seen.has(k)) await d.delete("patients", k);
  return loaded;
}

export async function downloadPackage(opts: DownloadOptions): Promise<{ patients: number; referenceChanged: boolean }> {
  opts.onProgress?.({ phase: "reference", loaded: 0, total: 0 });
  const ref = await downloadReference(opts);
  const patients = await downloadPatients(opts);
  await setMeta("package_at", Date.now());
  opts.onProgress?.({ phase: "done", loaded: patients, total: patients });
  return { patients, referenceChanged: ref.changed };
}
