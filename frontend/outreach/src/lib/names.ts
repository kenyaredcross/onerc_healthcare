/**
 * Name normalisation and phonetic keys, ported from onerc_healthcare/registration/names.py so the
 * device groups names the same way as the server's duplicate check.
 */
const SOUNDEX: Record<string, string> = {};
for (const [letters, code] of [
  ["BFPV", "1"],
  ["CGJKQSXZ", "2"],
  ["DT", "3"],
  ["L", "4"],
  ["MN", "5"],
  ["R", "6"],
] as const) {
  for (const c of letters) SOUNDEX[c] = code;
}

/** Lower case, punctuation removed, whitespace collapsed: `  O'Brien-Ali ` -> `obrien ali`. */
export function normaliseName(name: string | null | undefined): string {
  return (name ?? "")
    .replace(/-/g, " ")
    .toLowerCase()
    .split(/\s+/)
    .map((p) => p.replace(/[^\p{L}\p{N}]+/gu, ""))
    .filter(Boolean)
    .join(" ");
}

function asciiLetters(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[^\x00-\x7f]/g, "")
    .toUpperCase()
    .replace(/[^A-Z]/g, "");
}

/** American Soundex of the Latin letters in `name` (4 characters), or "" if it has none. */
export function soundex(name: string | null | undefined): string {
  const letters = asciiLetters(name ?? "");
  if (!letters) return "";
  let out = letters[0]!;
  let prev = SOUNDEX[letters[0]!] ?? "";
  for (const c of letters.slice(1)) {
    const code = SOUNDEX[c] ?? "";
    if (code && code !== prev) {
      out += code;
      if (out.length === 4) break;
    }
    if (c !== "H" && c !== "W") prev = code;
  }
  return out.padEnd(4, "0");
}

/** Phonetic key of a name; names without Latin letters fall back to their normalised text. */
export function nameKey(name: string | null | undefined): string {
  const key = soundex(name);
  if (key) return key;
  const n = normaliseName(name).replace(/ /g, "");
  return n ? `~${n.slice(0, 30)}` : "";
}

/** Identifier value as compared on the device: upper case, no spaces, dashes or slashes. */
export function normaliseIdentifier(value: string | null | undefined): string {
  return (value ?? "").toUpperCase().replace(/[\s\-/.]+/g, "");
}

/** difflib.SequenceMatcher(None, a, b).ratio() (Ratcliff/Obershelp, no junk heuristic). */
export function similarityRatio(a: string, b: string): number {
  if (!a.length && !b.length) return 1;
  const matches = (aLo: number, aHi: number, bLo: number, bHi: number): number => {
    let best = 0;
    let bi = 0;
    let bj = 0;
    for (let i = aLo; i < aHi; i++) {
      for (let j = bLo; j < bHi; j++) {
        let k = 0;
        while (i + k < aHi && j + k < bHi && a[i + k] === b[j + k]) k++;
        if (k > best) {
          best = k;
          bi = i;
          bj = j;
        }
      }
    }
    if (!best) return 0;
    return best + matches(aLo, bi, bLo, bj) + matches(bi + best, aHi, bj + best, bHi);
  };
  return (2 * matches(0, a.length, 0, b.length)) / (a.length + b.length);
}
