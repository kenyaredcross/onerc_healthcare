import {
  PBKDF2_ITERATIONS,
  WrongPinError,
  decryptJson,
  encryptJson,
  fromBase64,
  generateRawDataKey,
  importKeys,
  indexToken,
  toBase64,
  unwrapWithPin,
  wrapWithPin,
} from "../src/lib/crypto";

describe("crypto (protocol §5, NFR-18)", () => {
  it("encrypts and decrypts JSON under the data key, bound to its storage slot", async () => {
    const keys = await importKeys(generateRawDataKey());
    const value = { first_name: "Amina", n: 3, nested: { ok: true } };
    const enc = await encryptJson(keys, value, "patients:abc");
    expect(enc.iv).toHaveLength(12);
    expect(new TextDecoder().decode(enc.ct)).not.toContain("Amina");
    expect(await decryptJson(keys, enc, "patients:abc")).toEqual(value);
    // moved to another slot or opened with another key: refused
    await expect(decryptJson(keys, enc, "patients:xyz")).rejects.toThrow();
    const other = await importKeys(generateRawDataKey());
    await expect(decryptJson(other, enc, "patients:abc")).rejects.toThrow();
  });

  it("wraps the data key with a PIN (PBKDF2-SHA-256, >= 310,000 iterations, random salt) and refuses a wrong PIN", async () => {
    const raw = generateRawDataKey();
    const wrapped = await wrapWithPin(raw, "123456");
    expect(wrapped.iterations).toBeGreaterThanOrEqual(310_000);
    expect(PBKDF2_ITERATIONS).toBe(310_000);
    expect(wrapped.salt).toHaveLength(16);
    expect(toBase64(await unwrapWithPin(wrapped, "123456"))).toBe(toBase64(raw));
    await expect(unwrapWithPin(wrapped, "654321")).rejects.toBeInstanceOf(WrongPinError);
    const again = await wrapWithPin(raw, "123456");
    expect(toBase64(again.salt)).not.toBe(toBase64(wrapped.salt));
    await expect(wrapWithPin(raw, "123456", 1000)).rejects.toThrow(/310000/);
  });

  it("derives deterministic HMAC index tokens that depend on the key", async () => {
    const raw = generateRawDataKey();
    const a = await importKeys(raw);
    const b = await importKeys(fromBase64(toBase64(raw)));
    const c = await importKeys(generateRawDataKey());
    const t = await indexToken(a, "n:amina");
    expect(t).toBe(await indexToken(b, "n:amina"));
    expect(t).not.toBe(await indexToken(c, "n:amina"));
    expect(t).not.toContain("amina");
    expect(t).toMatch(/^[A-Za-z0-9_-]{22}$/);
  });
});
