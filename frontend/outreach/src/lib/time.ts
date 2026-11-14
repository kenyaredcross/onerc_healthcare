/**
 * Site time helpers. `captured_at` and every other wire timestamp is the site's local time
 * ("YYYY-MM-DD HH:mm:ss", Africa/Nairobi by default), whatever the device's own timezone is.
 */
export const DEFAULT_TIMEZONE = "Africa/Nairobi";

let siteTimezone = DEFAULT_TIMEZONE;

export function setSiteTimezone(tz: string | null | undefined): void {
  if (tz) {
    try {
      new Intl.DateTimeFormat("en-GB", { timeZone: tz });
      siteTimezone = tz;
    } catch {
      siteTimezone = DEFAULT_TIMEZONE;
    }
  }
}

export function getSiteTimezone(): string {
  return siteTimezone;
}

function parts(epochMs: number, tz: string): Record<string, string> {
  const fmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });
  const out: Record<string, string> = {};
  for (const p of fmt.formatToParts(new Date(epochMs))) out[p.type] = p.value;
  return out;
}

/** Site local time of an epoch, "YYYY-MM-DD HH:mm:ss". */
export function formatSiteTime(epochMs: number, tz: string = siteTimezone): string {
  const p = parts(epochMs, tz);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
}

export function siteNow(now: number = Date.now(), tz: string = siteTimezone): string {
  return formatSiteTime(now, tz);
}

export function siteToday(now: number = Date.now(), tz: string = siteTimezone): string {
  return formatSiteTime(now, tz).slice(0, 10);
}

/** Epoch ms of a site-local "YYYY-MM-DD[ HH:mm:ss[.ffffff]]" string. */
export function parseSiteTime(value: string, tz: string = siteTimezone): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(value.trim());
  if (!m) return Number.NaN;
  const [y, mo, d, h, mi, s] = [m[1], m[2], m[3], m[4] ?? "0", m[5] ?? "0", m[6] ?? "0"].map(Number) as number[];
  const asUtc = Date.UTC(y!, mo! - 1, d!, h!, mi!, s!);
  // offset of the zone at that instant (two passes handle DST edges)
  let guess = asUtc;
  for (let i = 0; i < 2; i++) {
    const p = parts(guess, tz);
    const shown = Date.UTC(+p.year!, +p.month! - 1, +p.day!, +p.hour!, +p.minute!, +p.second!);
    guess = guess + (asUtc - shown);
  }
  return guess;
}

/** Age in whole years and months at `on` (YYYY-MM-DD) for a DOB (YYYY-MM-DD). */
export function ageOn(dob: string, on: string): { years: number; months: number } {
  const [by, bm, bd] = dob.split("-").map(Number) as [number, number, number];
  const [oy, om, od] = on.split("-").map(Number) as [number, number, number];
  let months = (oy - by) * 12 + (om - bm);
  if (od < bd) months -= 1;
  months = Math.max(0, months);
  return { years: Math.floor(months / 12), months: months % 12 };
}

export function ageLabel(dob: string | null, estimated: boolean, on: string): string {
  if (!dob) return "";
  const { years, months } = ageOn(dob, on);
  const text = years >= 2 ? `${years} y` : `${years * 12 + months} m`;
  return estimated ? `~${text}` : text;
}
