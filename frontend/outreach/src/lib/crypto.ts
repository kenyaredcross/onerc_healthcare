/**
 * Device cryptography (protocol §5, NFR-18) on WebCrypto only.
 *
 * - Data key: a random AES-GCM 256 key. Every IndexedDB value is encrypted under it.
 * - PIN wrap: the raw data key is encrypted with an AES-GCM key derived from the PIN with
 *   PBKDF2-SHA-256 (>= 310,000 iterations, random 16-byte salt). Only this wrapped form is kept on the
 *   device; the raw key is also escrowed on the server at enrolment.
 * - Index key: an HMAC-SHA-256 key derived from the data key with HKDF, used for search tokens so
 *   names and identifiers never appear in plain text in an index.
 */

export const PBKDF2_ITERATIONS = 310_000;
const HKDF_INFO_INDEX = new TextEncoder().encode("krcs-outreach/index-v1");
const HKDF_SALT = new TextEncoder().encode("krcs-outreach");

export interface Keys {
  data: CryptoKey; // AES-GCM, not extractable
  index: CryptoKey; // HMAC-SHA-256, not extractable
}

export interface Encrypted {
  iv: Uint8Array;
  ct: Uint8Array;
}

export interface WrappedKey {
  v: 1;
  salt: Uint8Array;
  iv: Uint8Array;
  ct: Uint8Array;
  iterations: number;
}

export class WrongPinError extends Error {
  constructor() {
    super("Wrong PIN");
    this.name = "WrongPinError";
  }
}

const subtle = (): SubtleCrypto => globalThis.crypto.subtle;
const enc = new TextEncoder();
const dec = new TextDecoder();

/** A fresh Uint8Array backed by a plain ArrayBuffer (what WebCrypto's BufferSource expects). */
const bytes = (n: number): Uint8Array<ArrayBuffer> => crypto.getRandomValues(new Uint8Array(new ArrayBuffer(n)));
const buf = (u: Uint8Array): Uint8Array<ArrayBuffer> => {
  const out = new Uint8Array(new ArrayBuffer(u.byteLength));
  out.set(u);
  return out;
};

export function randomBytes(n: number): Uint8Array {
  return bytes(n);
}

export function toBase64(data: Uint8Array): string {
  let s = "";
  for (let i = 0; i < data.length; i += 0x8000) {
    s += String.fromCharCode(...data.subarray(i, i + 0x8000));
  }
  return btoa(s);
}

export function fromBase64(b64: string): Uint8Array {
  const s = atob(b64.trim());
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

function toBase64Url(data: Uint8Array): string {
  return toBase64(data).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** A new random 256-bit data key, raw bytes. Kept in memory only long enough to wrap / escrow it. */
export function generateRawDataKey(): Uint8Array {
  return bytes(32);
}

/** Import raw data key bytes into the in-memory key pair used by the app. */
export async function importKeys(raw: Uint8Array): Promise<Keys> {
  if (raw.length !== 32) throw new Error("The data key must be 256 bits");
  const r = buf(raw);
  const data = await subtle().importKey("raw", r, { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
  const hkdf = await subtle().importKey("raw", r, "HKDF", false, ["deriveKey"]);
  const index = await subtle().deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: HKDF_SALT, info: HKDF_INFO_INDEX },
    hkdf,
    { name: "HMAC", hash: "SHA-256", length: 256 },
    false,
    ["sign"],
  );
  return { data, index };
}

async function pinKey(pin: string, salt: Uint8Array, iterations: number): Promise<CryptoKey> {
  const base = await subtle().importKey("raw", enc.encode(pin), "PBKDF2", false, ["deriveKey"]);
  return subtle().deriveKey(
    { name: "PBKDF2", hash: "SHA-256", salt: buf(salt), iterations },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

/** Wrap the raw data key under a PIN (PBKDF2-SHA-256, random salt). */
export async function wrapWithPin(raw: Uint8Array, pin: string, iterations = PBKDF2_ITERATIONS): Promise<WrappedKey> {
  if (iterations < PBKDF2_ITERATIONS) throw new Error(`PBKDF2 needs at least ${PBKDF2_ITERATIONS} iterations`);
  const salt = bytes(16);
  const iv = bytes(12);
  const key = await pinKey(pin, salt, iterations);
  const ct = new Uint8Array(await subtle().encrypt({ name: "AES-GCM", iv }, key, buf(raw)));
  return { v: 1, salt, iv, ct, iterations };
}

/** Unwrap the raw data key with a PIN. Throws WrongPinError when the PIN is wrong. */
export async function unwrapWithPin(wrapped: WrappedKey, pin: string): Promise<Uint8Array> {
  const key = await pinKey(pin, wrapped.salt, wrapped.iterations);
  try {
    return new Uint8Array(await subtle().decrypt({ name: "AES-GCM", iv: buf(wrapped.iv) }, key, buf(wrapped.ct)));
  } catch {
    throw new WrongPinError();
  }
}

/** Encrypt a JSON value. `aad` binds the ciphertext to its storage slot (store + key). */
export async function encryptJson(keys: Keys, value: unknown, aad: string): Promise<Encrypted> {
  const iv = bytes(12);
  const ct = await subtle().encrypt(
    { name: "AES-GCM", iv, additionalData: enc.encode(aad) },
    keys.data,
    enc.encode(JSON.stringify(value)),
  );
  return { iv, ct: new Uint8Array(ct) };
}

export async function decryptJson<T>(keys: Keys, value: Encrypted, aad: string): Promise<T> {
  const pt = await subtle().decrypt(
    { name: "AES-GCM", iv: buf(value.iv), additionalData: enc.encode(aad) },
    keys.data,
    buf(value.ct),
  );
  return JSON.parse(dec.decode(pt)) as T;
}

/** HMAC-SHA-256 search token (first 128 bits, base64url). Never reversible to the plain value. */
export async function indexToken(keys: Keys, value: string): Promise<string> {
  const sig = await subtle().sign("HMAC", keys.index, enc.encode(value));
  return toBase64Url(new Uint8Array(sig).subarray(0, 16));
}
