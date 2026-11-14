/**
 * ICD-11 search over the package's local index (reference.icd11 rows `[name, code, display, is_leaf]`).
 * Ranking: exact code > code prefix > title prefix (any word) > contains (code or title); within a rank
 * leaf codes (codable) first, then by code.
 */
import type { IcdRow } from "./protocol";

export interface IcdEntry {
  name: string; // Code Value name, sent in the payload
  code: string;
  title: string;
  leaf: boolean;
  codeUpper: string;
  titleLower: string;
}

export interface IcdHit extends IcdEntry {
  rank: number; // 0 exact code, 1 code prefix, 2 title prefix, 3 contains
}

export function buildIcdIndex(rows: IcdRow[]): IcdEntry[] {
  return rows.map(([name, code, title, leaf]) => ({
    name,
    code,
    title,
    leaf: Boolean(leaf),
    codeUpper: (code ?? "").toUpperCase(),
    titleLower: (title ?? "").toLowerCase(),
  }));
}

function titlePrefix(titleLower: string, q: string): boolean {
  if (titleLower.startsWith(q)) return true;
  // a word of the title starts with the query ("malaria" matches "Plasmodium falciparum malaria")
  return titleLower.includes(` ${q}`) || titleLower.includes(`(${q}`) || titleLower.includes(`-${q}`);
}

export function searchIcd(index: IcdEntry[], query: string, limit = 30): IcdHit[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const qUpper = q.toUpperCase();
  const hits: IcdHit[] = [];
  for (const e of index) {
    let rank: number;
    if (e.codeUpper === qUpper) rank = 0;
    else if (e.codeUpper.startsWith(qUpper)) rank = 1;
    else if (titlePrefix(e.titleLower, q)) rank = 2;
    else if (e.titleLower.includes(q) || e.codeUpper.includes(qUpper)) rank = 3;
    else continue;
    hits.push({ ...e, rank });
  }
  hits.sort(
    (a, b) =>
      a.rank - b.rank ||
      Number(b.leaf) - Number(a.leaf) ||
      a.code.length - b.code.length ||
      a.code.localeCompare(b.code),
  );
  return hits.slice(0, limit);
}
