/** The package reference data, decrypted once per unlock and kept in memory. */
import type { Keys } from "./crypto";
import { getReference } from "./db";
import { buildIcdIndex, type IcdEntry } from "./icd";
import type { ReferenceData } from "./protocol";

export interface LoadedReference extends ReferenceData {
  icdIndex: IcdEntry[];
  icdByName: Map<string, IcdEntry>;
}

let cache: { keys: Keys; ref: LoadedReference } | null = null;

export function clearReferenceCache(): void {
  cache = null;
}

export async function loadReference(keys: Keys): Promise<LoadedReference | null> {
  if (cache && cache.keys === keys) return cache.ref;
  const data = await getReference<ReferenceData>(keys);
  if (!data) return null;
  const icdIndex = buildIcdIndex(data.icd11 ?? []);
  const ref: LoadedReference = { ...data, icdIndex, icdByName: new Map(icdIndex.map((e) => [e.name, e])) };
  cache = { keys, ref };
  return ref;
}

/** Residence path "County / Camp / Village" for display. */
export function residencePath(ref: ReferenceData, name: string | null | undefined): string {
  if (!name) return "";
  const byName = new Map(ref.residences.map((r) => [r.name, r]));
  const parts: string[] = [];
  let cur = byName.get(name);
  let guard = 0;
  while (cur && guard++ < 20) {
    parts.unshift(cur.location_name || cur.name);
    cur = cur.parent ? byName.get(cur.parent) : undefined;
  }
  return parts.length ? parts.join(" / ") : name;
}
