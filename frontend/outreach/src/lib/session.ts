/**
 * Device session: online sign-in, enrolment with data-key escrow, PIN set/unlock, lockout and
 * recovery, expiry (TST-10) and the idle lock (protocol §1, §2, §5; OUT-07/08, NFR-18).
 *
 * Plain in IndexedDB `meta` (needed while locked): device_id, the profile (user, session, grant name,
 * expiry, PIN rules, sync settings), the PIN-wrapped data key and the wrong-PIN counter.
 * Encrypted (`secure:*`): the sync secret, the session summary and a key-check value.
 * In memory only: the unlocked keys, and the raw data key between enrolment/recovery and "Set PIN".
 */
import {
  ApiError,
  callMethod,
  fetchBoot,
  login,
  outreachMethod,
  setCsrfToken,
  type DeviceCredentials,
} from "./api";
import {
  WrongPinError,
  fromBase64,
  generateRawDataKey,
  importKeys,
  toBase64,
  unwrapWithPin,
  wrapWithPin,
  type Keys,
  type WrappedKey,
} from "./crypto";
import { db, delMeta, getMeta, getSecure, setMeta, setSecure, wipeAll } from "./db";
import {
  DEFAULT_SETTINGS,
  type DeviceSettings,
  type EnrolResponse,
  type OutreachSessionSummary,
  type RecoverKeyResponse,
} from "./protocol";
import { noteRefusal, refreshSessionStatus } from "./sessionStatus";
import { parseSiteTime } from "./time";

export const IDLE_LOCK_MS = 15 * 60 * 1000;

export interface Profile {
  user: string;
  full_name: string;
  session: string;
  session_title: string;
  facility_name: string;
  grant: string;
  expires_at: string; // site time, as sent by the server
  expires_epoch: number;
  settings: DeviceSettings;
  enrolled_at: number;
}

export type LockState =
  | "new" // no profile: online sign-in, pick a session, enrol
  | "needs-pin" // keys in memory, no PIN yet
  | "locked" // PIN unlock possible
  | "expired" // offline period over: online sign-in, then PIN (re-enrol)
  | "locked-out" // too many wrong PINs: online sign-in, then key recovery
  | "unlocked";

export class ExpiredError extends Error {
  constructor() {
    super("The offline period has ended. Sign in online to continue.");
    this.name = "ExpiredError";
  }
}

export class LockedOutError extends Error {
  constructor() {
    super("Too many wrong PINs. Sign in online to recover access; your records are safe.");
    this.name = "LockedOutError";
  }
}

export class PinError extends Error {
  remaining: number;
  constructor(remaining: number) {
    super(`Wrong PIN. ${remaining} attempt${remaining === 1 ? "" : "s"} left.`);
    this.name = "PinError";
    this.remaining = remaining;
  }
}

export class WrongUserError extends Error {
  constructor(expected: string) {
    super(`This device holds records of ${expected}. Sign in as that user, or sync and reset the device first.`);
    this.name = "WrongUserError";
  }
}

// ----- in-memory state -----------------------------------------------------------------------

interface MemoryState {
  keys: Keys | null;
  rawForPin: Uint8Array | null;
  onlineUser: { name: string; full_name: string } | null;
}

const mem: MemoryState = { keys: null, rawForPin: null, onlineUser: null };
const listeners = new Set<() => void>();

export function onSessionChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
const changed = (): void => listeners.forEach((fn) => fn());

export function getKeys(): Keys | null {
  return mem.keys;
}

export function requireKeys(): Keys {
  if (!mem.keys) throw new Error("The app is locked");
  return mem.keys;
}

export function onlineUser(): { name: string; full_name: string } | null {
  return mem.onlineUser;
}

/** Forget everything held in memory (tests; also used by lock()). */
export function resetMemory(): void {
  mem.keys = null;
  mem.rawForPin = null;
  mem.onlineUser = null;
}

// ----- device id / profile -------------------------------------------------------------------

export async function getDeviceId(): Promise<string> {
  let id = await getMeta<string>("device_id");
  if (!id) {
    id = crypto.randomUUID();
    await setMeta("device_id", id);
  }
  return id;
}

export function getProfile(): Promise<Profile | undefined> {
  return getMeta<Profile>("profile");
}

export async function getSettings(): Promise<DeviceSettings> {
  return (await getProfile())?.settings ?? DEFAULT_SETTINGS;
}

export async function lockState(now: number = Date.now()): Promise<LockState> {
  const profile = await getProfile();
  if (!profile) return "new";
  if (mem.keys && mem.rawForPin) return "needs-pin";
  const wrapped = await getMeta<WrappedKey>("wrapped_key");
  if (!wrapped) return "locked-out";
  // Expiry wins over an unlocked app: the next screen asks for the online sign-in (TST-10).
  if (now >= profile.expires_epoch) return "expired";
  if (mem.keys) return "unlocked";
  return "locked";
}

export async function isAuthRequired(): Promise<boolean> {
  return Boolean(await getMeta<boolean>("auth_required"));
}

/** A device call got 401: keep every record, ask for an online sign-in (protocol §1). */
export async function markAuthRequired(): Promise<void> {
  await setMeta("auth_required", true);
  changed();
}

// ----- online -------------------------------------------------------------------------------

export async function signInOnline(usr: string, pwd: string): Promise<{ name: string; full_name: string }> {
  await login(usr, pwd);
  const boot = await fetchBoot();
  setCsrfToken(boot.csrf_token);
  if (!boot.user) throw new ApiError(401, "Sign-in did not start a session. Check that cookies are allowed.");
  mem.onlineUser = boot.user;
  await checkSessionStatus();
  changed();
  return boot.user;
}

/** Online: learn whether this device's session is still open (F1). No-op without a profile. */
export async function checkSessionStatus(): Promise<void> {
  const profile = await getProfile();
  if (profile && mem.onlineUser) await refreshSessionStatus(profile.session);
}

/** Adopt an existing browser session (the page boot said a user is signed in). */
export function adoptOnlineUser(user: { name: string; full_name: string } | null, csrf: string | null): void {
  if (user && user.name !== "Guest") {
    mem.onlineUser = user;
    setCsrfToken(csrf);
  }
}

export function mySessions(): Promise<OutreachSessionSummary[]> {
  return callMethod<OutreachSessionSummary[]>(outreachMethod("devices.my_sessions"));
}

export function defaultDeviceLabel(): string {
  const ua = typeof navigator !== "undefined" ? navigator.userAgent : "";
  const os = /Android/i.test(ua)
    ? "Android"
    : /iPhone|iPad|iPod/i.test(ua)
      ? "iOS"
      : /Windows/i.test(ua)
        ? "Windows"
        : /Mac OS/i.test(ua)
          ? "macOS"
          : /Linux/i.test(ua)
            ? "Linux"
            : "device";
  const browser = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "browser";
  return `${browser} on ${os}`;
}

async function enrolWith(
  session: OutreachSessionSummary | { name: string; title: string; facility_name: string },
  raw: Uint8Array,
  label: string,
): Promise<Profile> {
  const deviceId = await getDeviceId();
  let res: EnrolResponse;
  try {
    res = await callMethod<EnrolResponse>(outreachMethod("devices.enrol"), {
      method: "POST",
      body: { session: session.name, device_id: deviceId, device_label: label, data_key: toBase64(raw) },
    });
  } catch (e) {
    // A Cancelled session refuses the renewal: remember it, so the app says who to contact (F1).
    if ((await getProfile())?.session === session.name) await noteRefusal(e);
    throw e;
  }
  const keys = await importKeys(raw);
  const profile: Profile = {
    user: res.user.name,
    full_name: res.user.full_name,
    session: session.name,
    session_title: session.title,
    facility_name: session.facility_name,
    grant: res.grant,
    expires_at: res.expires_at,
    expires_epoch: parseSiteTime(res.expires_at),
    settings: { ...DEFAULT_SETTINGS, ...res.settings },
    enrolled_at: Date.now(),
  };
  await setSecure(keys, "secret", res.secret);
  await setSecure(keys, "check", "krcs-outreach");
  await setMeta("profile", profile);
  await delMeta("auth_required");
  mem.keys = keys;
  return profile;
}

async function pendingOutboxCount(): Promise<number> {
  const rows = await (await db()).getAll("outbox");
  return rows.filter((r) => !r.final).length;
}

/**
 * First enrolment for a session (after an online sign-in). A device that already holds another
 * profile is reset first, which is only allowed when nothing is waiting to be synced.
 */
export async function enrolNew(session: OutreachSessionSummary, label: string): Promise<Profile> {
  const user = mem.onlineUser;
  if (!user) throw new ApiError(401, "Sign in online first.");
  const existing = await getProfile();
  if (existing) {
    if ((await pendingOutboxCount()) > 0) {
      throw new Error("Records on this device are not synced yet. Sync them before switching session or user.");
    }
    await wipeAll(["device_id"]);
  }
  const raw = generateRawDataKey();
  const profile = await enrolWith(session, raw, label);
  mem.rawForPin = raw;
  changed();
  return profile;
}

function validatePin(pin: string, settings: DeviceSettings): void {
  if (!/^\d+$/.test(pin)) throw new Error("The PIN must contain digits only.");
  if (pin.length < settings.pin_min_length) {
    throw new Error(`The PIN must have at least ${settings.pin_min_length} digits.`);
  }
}

/** Set (or reset after recovery) the PIN that wraps the data key. */
export async function setPin(pin: string, iterations?: number): Promise<void> {
  const profile = await getProfile();
  if (!profile || !mem.rawForPin || !mem.keys) throw new Error("Nothing to protect with a PIN yet.");
  validatePin(pin, profile.settings);
  const wrapped = await wrapWithPin(mem.rawForPin, pin, iterations);
  await setMeta("wrapped_key", wrapped);
  await setMeta("pin_attempts", 0);
  mem.rawForPin.fill(0);
  mem.rawForPin = null;
  changed();
}

/** Check the PIN; counts wrong attempts and discards the wrapped key at `max_pin_attempts`. */
async function unwrap(pin: string, profile: Profile): Promise<Uint8Array> {
  const wrapped = await getMeta<WrappedKey>("wrapped_key");
  if (!wrapped) throw new LockedOutError();
  try {
    const raw = await unwrapWithPin(wrapped, pin);
    await setMeta("pin_attempts", 0);
    return raw;
  } catch (e) {
    if (!(e instanceof WrongPinError)) throw e;
    const attempts = ((await getMeta<number>("pin_attempts")) ?? 0) + 1;
    const max = profile.settings.max_pin_attempts;
    if (attempts >= max) {
      await delMeta("wrapped_key");
      await setMeta("pin_attempts", 0);
      changed();
      throw new LockedOutError();
    }
    await setMeta("pin_attempts", attempts);
    throw new PinError(max - attempts);
  }
}

/** Offline unlock with the PIN. Refused after `expires_at` (TST-10) and after a lockout. */
export async function unlock(pin: string, now: number = Date.now()): Promise<void> {
  const profile = await getProfile();
  if (!profile) throw new Error("This device is not enrolled.");
  if (!(await getMeta<WrappedKey>("wrapped_key"))) throw new LockedOutError();
  if (now >= profile.expires_epoch) throw new ExpiredError();
  const raw = await unwrap(pin, profile);
  mem.keys = await importKeys(raw);
  raw.fill(0);
  changed();
}

function assertSameUser(profile: Profile): void {
  const user = mem.onlineUser;
  if (!user) throw new ApiError(401, "Sign in online first.");
  if (user.name !== profile.user) throw new WrongUserError(profile.full_name || profile.user);
}

/**
 * After the offline period ended (or a 401): online sign-in done, the PIN unlocks the key and the
 * device enrols again: new secret and expiry, same data key (protocol §5 "Expiry").
 */
export async function renew(pin: string, label: string = defaultDeviceLabel()): Promise<Profile> {
  const profile = await getProfile();
  if (!profile) throw new Error("This device is not enrolled.");
  assertSameUser(profile);
  const raw = await unwrap(pin, profile);
  try {
    const renewed = await enrolWith(
      { name: profile.session, title: profile.session_title, facility_name: profile.facility_name },
      raw,
      label,
    );
    changed();
    return renewed;
  } finally {
    raw.fill(0);
  }
}

/**
 * After a PIN lockout: online sign-in done, fetch the escrowed key (`recover_key`), check it opens the
 * device data, re-enrol if the offline period is over, then the user sets a new PIN.
 */
export async function recoverKey(label: string = defaultDeviceLabel(), now: number = Date.now()): Promise<void> {
  const profile = await getProfile();
  if (!profile) throw new Error("This device is not enrolled.");
  assertSameUser(profile);
  const res = await callMethod<RecoverKeyResponse>(outreachMethod("devices.recover_key"), {
    params: { grant: profile.grant },
  });
  const raw = fromBase64(res.data_key);
  const keys = await importKeys(raw);
  const check = await getSecure<string>(keys, "check").catch(() => undefined);
  if (check !== "krcs-outreach") throw new Error("The recovered key does not open this device's records.");
  if (now >= profile.expires_epoch || (await isAuthRequired())) {
    await enrolWith(
      { name: profile.session, title: profile.session_title, facility_name: profile.facility_name },
      raw,
      label,
    );
  }
  mem.keys = keys;
  mem.rawForPin = raw;
  changed();
}

export function lock(): void {
  mem.keys = null;
  if (mem.rawForPin) mem.rawForPin.fill(0);
  mem.rawForPin = null;
  changed();
}

/** Sync credentials (needs the unlocked keys: the secret is encrypted). */
export async function deviceCredentials(): Promise<DeviceCredentials> {
  const profile = await getProfile();
  const keys = requireKeys();
  if (!profile) throw new Error("This device is not enrolled.");
  const secret = await getSecure<string>(keys, "secret");
  if (!secret) throw new Error("No sync secret on this device. Sign in online again.");
  return { grant: profile.grant, secret };
}

/** Remove this device's data (only when nothing is waiting to be synced). */
export async function resetDevice(): Promise<void> {
  if ((await pendingOutboxCount()) > 0) {
    throw new Error("Records on this device are not synced yet.");
  }
  await wipeAll(["device_id"]);
  lock();
}

/**
 * Fix round 1 (F1): after the session was closed (or cancelled) and every record has a final result,
 * prepare the device for a new session. Removes the profile, the package, the acknowledged records and
 * the keys (no patient data stays); keeps the device id and the online sign-in, so the next screen is
 * the session picker. Refused while any record is not final or a visit is unfinished: nothing is lost.
 */
export async function startNewSession(): Promise<void> {
  const pending = await pendingOutboxCount();
  if (pending > 0) {
    throw new Error(`${pending} record(s) on this device have no final result yet. They are kept; nothing is deleted.`);
  }
  const drafts = await (await db()).count("drafts");
  if (drafts > 0) {
    throw new Error(`${drafts} unfinished visit(s) are on this device. Finish them (they still sync) or discard them first.`);
  }
  await wipeAll(["device_id"]);
  lock();
}

/**
 * Team lead only (server-checked): mark the session Returned when the team is back. Needs the online
 * cookie session (CSRF header), never the device grant.
 */
export async function markSessionReturned(): Promise<{ name: string; status: string }> {
  const profile = await getProfile();
  if (!profile) throw new Error("This device is not enrolled.");
  if (!mem.onlineUser) throw new ApiError(401, "Sign in online first.");
  return callMethod<{ name: string; status: string }>("onerc_healthcare.outreach.session.apply_action", {
    method: "POST",
    body: { session: profile.session, action: "return" },
  });
}

// ----- idle lock ------------------------------------------------------------------------------

/** Locks the app after `timeoutMs` without user activity (NFR-18). */
export class IdleLock {
  private timer: ReturnType<typeof setTimeout> | null = null;
  constructor(
    private readonly onIdle: () => void,
    private readonly timeoutMs: number = IDLE_LOCK_MS,
  ) {}
  touch = (): void => {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.onIdle();
    }, this.timeoutMs);
  };
  stop(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}
