/**
 * Patients on the device: package patients plus those registered here. Rows are encrypted; the search
 * index holds only HMAC tokens of normalised name parts (and their prefixes), phonetic keys and
 * identifier values, so a stolen database reveals no names.
 */
import { decryptJson, encryptJson, indexToken, type Keys } from "./crypto";
import { aad, db, type PatientRow } from "./db";
import { nameKey, normaliseIdentifier, normaliseName, similarityRatio } from "./names";
import type { Identifier, PackagePatient, PackagePrescription, PackageVisit } from "./protocol";

export interface OutreachVisitSummary {
  client_uuid: string;
  date: string;
  department: string;
  diagnoses: { code: string; title: string; case_type: string }[];
  prescriptions: string[];
}

export interface LocalPatient {
  key: string;
  name: string | null; // server Patient name
  client_uuid: string | null; // patient.register uuid when registered on this device
  modified: string | null; // base version for patient.update
  first_name: string;
  middle_name: string | null;
  last_name: string | null;
  sex: string | null;
  dob: string | null;
  dob_estimated: number;
  mobile: string | null;
  residence: string | null;
  allergies: string | null;
  is_walk_in?: number;
  identifiers: Identifier[];
  recent_visits: PackageVisit[];
  active_prescriptions: PackagePrescription[];
  outreach_visits: OutreachVisitSummary[];
  pending_updates: number; // patient.update records not yet final
}

export function fullName(p: Pick<LocalPatient, "first_name" | "middle_name" | "last_name">): string {
  return [p.first_name, p.middle_name, p.last_name].filter(Boolean).join(" ");
}

const MIN_PREFIX = 2;
const MAX_PREFIX = 20;
const MIN_ID_PREFIX = 3;

/** Plain (pre-HMAC) search terms of a patient. */
export function patientTerms(p: Pick<LocalPatient, "first_name" | "middle_name" | "last_name" | "identifiers" | "name">): string[] {
  const terms = new Set<string>();
  for (const part of normaliseName(fullName(p)).split(" ").filter(Boolean)) {
    for (let n = MIN_PREFIX; n <= Math.min(part.length, MAX_PREFIX); n++) terms.add(`n:${part.slice(0, n)}`);
    if (part.length < MIN_PREFIX) terms.add(`n:${part}`);
    const k = nameKey(part);
    if (k) terms.add(`p:${k}`);
  }
  const ids = [...p.identifiers.map((i) => i.identifier_value), ...(p.name ? [p.name] : [])];
  for (const raw of ids) {
    const v = normaliseIdentifier(raw);
    for (let n = MIN_ID_PREFIX; n <= Math.min(v.length, 32); n++) terms.add(`i:${v.slice(0, n)}`);
    if (v && v.length < MIN_ID_PREFIX) terms.add(`i:${v}`);
  }
  return [...terms];
}

async function tokens(keys: Keys, terms: string[]): Promise<string[]> {
  return Promise.all(terms.map((t) => indexToken(keys, t)));
}

export async function patientKey(keys: Keys, p: { name: string | null; client_uuid: string | null }): Promise<string> {
  return indexToken(keys, p.name ? `pat:${p.name}` : `new:${p.client_uuid}`);
}

export async function putPatient(keys: Keys, p: LocalPatient): Promise<void> {
  const row: PatientRow = {
    key: p.key,
    tokens: await tokens(keys, patientTerms(p)),
    local: p.name ? 0 : 1,
    enc: await encryptJson(keys, p, aad("patients", p.key)),
  };
  await (await db()).put("patients", row);
}

export async function getPatient(keys: Keys, key: string): Promise<LocalPatient | null> {
  const row = await (await db()).get("patients", key);
  return row ? decryptJson<LocalPatient>(keys, row.enc, aad("patients", key)) : null;
}

export async function countPatients(): Promise<number> {
  return (await db()).count("patients");
}

export function fromPackage(key: string, p: PackagePatient): LocalPatient {
  return {
    key,
    name: p.name,
    client_uuid: null,
    modified: p.modified,
    first_name: p.first_name,
    middle_name: p.middle_name,
    last_name: p.last_name,
    sex: p.sex,
    dob: p.dob,
    dob_estimated: p.dob_estimated ?? 0,
    mobile: p.mobile,
    residence: p.residence,
    allergies: p.allergies,
    identifiers: p.identifiers ?? [],
    recent_visits: p.recent_visits ?? [],
    active_prescriptions: p.active_prescriptions ?? [],
    outreach_visits: [],
    pending_updates: 0,
  };
}

async function keysForToken(token: string): Promise<string[]> {
  return (await (await db()).getAllKeysFromIndex("patients", "tokens", token)) as string[];
}

export interface SearchHit {
  patient: LocalPatient;
  score: number;
}

/** Client-side patient search on name parts (prefix or phonetic) and identifiers. */
export async function searchPatients(keys: Keys, query: string, limit = 50): Promise<SearchHit[]> {
  const words = normaliseName(query).split(" ").filter(Boolean);
  const idQuery = normaliseIdentifier(query);
  if (!words.length && !idQuery) return [];

  const perWord: Set<string>[] = [];
  for (const w of words) {
    const terms = [`n:${w.slice(0, MAX_PREFIX)}`];
    if (w.length >= 3) {
      const k = nameKey(w);
      if (k) terms.push(`p:${k}`);
    }
    const iv = normaliseIdentifier(w);
    if (iv.length >= MIN_ID_PREFIX) terms.push(`i:${iv.slice(0, 32)}`);
    const found = new Set<string>();
    for (const t of await tokens(keys, terms)) for (const k of await keysForToken(t)) found.add(k);
    perWord.push(found);
  }
  let candidates = perWord.length
    ? perWord.reduce((acc, s) => new Set([...acc].filter((k) => s.has(k))))
    : new Set<string>();
  if (idQuery.length >= MIN_ID_PREFIX) {
    for (const k of await keysForToken(await indexToken(keys, `i:${idQuery.slice(0, 32)}`))) candidates.add(k);
  }
  if (candidates.size > 500) candidates = new Set([...candidates].slice(0, 500));

  const hits: SearchHit[] = [];
  for (const key of candidates) {
    const p = await getPatient(keys, key);
    if (!p) continue;
    hits.push({ patient: p, score: scoreMatch(p, words, idQuery) });
  }
  hits.sort((a, b) => b.score - a.score || fullName(a.patient).localeCompare(fullName(b.patient)));
  return hits.slice(0, limit);
}

function scoreMatch(p: LocalPatient, words: string[], idQuery: string): number {
  const parts = normaliseName(fullName(p)).split(" ");
  let score = 0;
  for (const w of words) {
    if (parts.includes(w)) score += 3;
    else if (parts.some((x) => x.startsWith(w))) score += 2;
    else if (parts.some((x) => nameKey(x) === nameKey(w))) score += 1;
  }
  const ids = [...p.identifiers.map((i) => normaliseIdentifier(i.identifier_value)), normaliseIdentifier(p.name)];
  if (idQuery && ids.includes(idQuery)) score += 10;
  else if (idQuery.length >= MIN_ID_PREFIX && ids.some((i) => i.startsWith(idQuery))) score += 4;
  return score;
}

// ----- similarity warning (port of registration/similarity.py) -------------------------------

export const MIN_SCORE = 4;
const EXACT = 3;
const SIMILAR = 2;

export interface SimilarityInput {
  first_name: string;
  last_name: string | null;
  sex: string | null;
  dob: string | null;
  dob_estimated: boolean;
  residence: string | null;
  identifiers: Identifier[];
}

export interface SimilarPatient {
  patient: LocalPatient;
  score: number;
  reasons: string[];
  sameIdentifier: boolean;
}

function nameScore(given: string, candidate: string): number {
  if (!given || !candidate) return 0;
  if (given === candidate) return EXACT;
  if (nameKey(given) === nameKey(candidate) || similarityRatio(given, candidate) >= 0.8) return SIMILAR;
  return 0;
}

function withinYears(a: string, b: string, years: number): boolean {
  const [ay, am, ad] = a.split("-").map(Number) as [number, number, number];
  const lo = `${String(ay - years).padStart(4, "0")}-${String(am).padStart(2, "0")}-${String(ad).padStart(2, "0")}`;
  const hi = `${String(ay + years).padStart(4, "0")}-${String(am).padStart(2, "0")}-${String(ad).padStart(2, "0")}`;
  return lo <= b && b <= hi;
}

/** Score one candidate as the server does; null when it is not a possible duplicate. */
export function scoreCandidate(input: SimilarityInput, c: LocalPatient): { score: number; reasons: string[] } | null {
  const first = normaliseName(input.first_name);
  const last = normaliseName(input.last_name);
  const cFirst = normaliseName(c.first_name);
  const cLast = normaliseName(c.last_name);
  const straight: [number, number] = [nameScore(first, cFirst), nameScore(last, cLast)];
  const swapped: [number, number] = [nameScore(first, cLast), nameScore(last, cFirst)];
  const isSwapped = swapped[0] + swapped[1] > straight[0] + straight[1];
  const pair = isSwapped ? swapped : straight;
  const nameTotal = pair[0] + pair[1];
  if (!nameTotal) return null;
  const reasons: string[] = [];
  const labels: [string, number][] = isSwapped
    ? [
        ["first and last name swapped", pair[0]],
        ["last and first name swapped", pair[1]],
      ]
    : [
        ["first name", pair[0]],
        ["last name", pair[1]],
      ];
  for (const [label, s] of labels) {
    if (s === EXACT) reasons.push(`same ${label}`);
    else if (s === SIMILAR) reasons.push(`similar ${label}`);
  }
  let score = nameTotal;
  if (input.sex && c.sex) {
    if (input.sex === c.sex) {
      score += 1;
      reasons.push("same sex");
    } else score -= 3;
  }
  if (input.dob && c.dob) {
    if (c.dob === input.dob) {
      score += 3;
      reasons.push("same date of birth");
    } else if (c.dob.slice(0, 4) === input.dob.slice(0, 4)) {
      score += 1;
      reasons.push("same year of birth");
    } else if ((input.dob_estimated || c.dob_estimated) && withinYears(input.dob, c.dob, 2)) {
      score += 1;
      reasons.push("date of birth within 2 years (estimated)");
    } else if (Math.abs(Number(c.dob.slice(0, 4)) - Number(input.dob.slice(0, 4))) > 5) {
      score -= 2;
    }
  }
  if (input.residence && c.residence && input.residence === c.residence) {
    score += 2;
    reasons.push("same residence");
  }
  return score >= MIN_SCORE ? { score, reasons } : null;
}

/** Possible duplicates among the patients on the device (warning before an offline registration). */
export async function findSimilar(keys: Keys, input: SimilarityInput, exclude?: string): Promise<SimilarPatient[]> {
  const terms: string[] = [];
  for (const n of [input.first_name, input.last_name]) {
    for (const part of normaliseName(n).split(" ").filter(Boolean)) {
      const k = nameKey(part);
      if (k) terms.push(`p:${k}`);
    }
  }
  const idValues = input.identifiers.map((i) => normaliseIdentifier(i.identifier_value)).filter(Boolean);
  for (const v of idValues) terms.push(`i:${v.slice(0, 32)}`);
  const keysFound = new Set<string>();
  for (const t of await tokens(keys, terms)) for (const k of await keysForToken(t)) keysFound.add(k);
  if (exclude) keysFound.delete(exclude);

  const out: SimilarPatient[] = [];
  for (const key of keysFound) {
    const c = await getPatient(keys, key);
    if (!c) continue;
    const sameIdentifier = input.identifiers.some((i) =>
      c.identifiers.some(
        (ci) =>
          ci.identifier_type === i.identifier_type &&
          normaliseIdentifier(ci.identifier_value) === normaliseIdentifier(i.identifier_value) &&
          normaliseIdentifier(i.identifier_value) !== "",
      ),
    );
    const scored = scoreCandidate(input, c);
    if (sameIdentifier) {
      out.push({ patient: c, score: (scored?.score ?? 0) + 100, reasons: ["same identifier", ...(scored?.reasons ?? [])], sameIdentifier });
    } else if (scored) {
      out.push({ patient: c, score: scored.score, reasons: scored.reasons, sameIdentifier: false });
    }
  }
  out.sort((a, b) => b.score - a.score);
  return out.slice(0, 10);
}
