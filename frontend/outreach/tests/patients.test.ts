import type { Keys } from "../src/lib/crypto";
import { db } from "../src/lib/db";
import { nameKey, normaliseName, similarityRatio, soundex } from "../src/lib/names";
import { allRows, readSecret } from "../src/lib/outbox";
import {
  findSimilar,
  fromPackage,
  getPatient,
  patientKey,
  putPatient,
  scoreCandidate,
  searchPatients,
  type LocalPatient,
} from "../src/lib/patients";
import type { PackagePatient } from "../src/lib/protocol";
import { estimateDob, registerPatient, updatePatient, validateRegister } from "../src/lib/records";
import { freshDevice, testKeys } from "./helpers";

let keys: Keys;

function pkg(name: string, first: string, last: string, extra: Partial<PackagePatient> = {}): PackagePatient {
  return {
    name,
    modified: "2026-11-10 08:01:02.123456",
    first_name: first,
    middle_name: null,
    last_name: last,
    sex: "Female",
    dob: "1990-05-01",
    dob_estimated: 0,
    mobile: null,
    residence: "Village 3",
    allergies: null,
    identifiers: [],
    recent_visits: [],
    active_prescriptions: [],
    ...extra,
  };
}

async function load(...patients: PackagePatient[]): Promise<LocalPatient[]> {
  const out = [];
  for (const p of patients) {
    const local = fromPackage(await patientKey(keys, { name: p.name, client_uuid: null }), p);
    await putPatient(keys, local);
    out.push(local);
  }
  return out;
}

const REF = {
  identifier_types: [
    { name: "UNHCR ID", is_primary: 1, pattern: "^[0-9]{3}-[0-9]{8}$", facility_generated: 0 },
    { name: "National ID", is_primary: 0, pattern: null, facility_generated: 0 },
    { name: "Facility-generated", is_primary: 0, pattern: null, facility_generated: 1 },
  ],
  genders: ["Female", "Male"],
};

describe("names (port of registration/names.py)", () => {
  it("normalises and keys names like the server", () => {
    expect(normaliseName("  O'Brien-Ali ")).toBe("obrien ali");
    expect(soundex("Robert")).toBe("R163");
    expect(soundex("Rupert")).toBe("R163");
    expect(soundex("Ashcraft")).toBe("A261");
    expect(soundex("Tymczak")).toBe("T522");
    expect(soundex("Mohammed")).toBe(soundex("Muhamad"));
    expect(nameKey("أحمد")).toBe("~أحمد");
    expect(similarityRatio("mohamed", "mohammed")).toBeCloseTo(14 / 15, 6);
    expect(similarityRatio("abc", "xyz")).toBe(0);
  });
});

describe("patients on the device", () => {
  beforeEach(async () => {
    await freshDevice();
    keys = await testKeys();
  });

  it("stores rows encrypted with HMAC search tokens only (no plain names or identifiers)", async () => {
    await load(pkg("KLB-PAT-2026-00012", "Amina", "Hassan", { identifiers: [{ identifier_type: "UNHCR ID", identifier_value: "123-45678901" }] }));
    const rows = await (await db()).getAll("patients");
    const raw = JSON.stringify(rows, (_k, v) => (v instanceof Uint8Array ? new TextDecoder().decode(v) : v));
    for (const secret of ["Amina", "amina", "Hassan", "hassan", "12345678901", "KLB-PAT"]) expect(raw).not.toContain(secret);
    expect(rows[0]!.tokens.length).toBeGreaterThan(5);
  });

  it("finds patients by name prefix, by a similar-sounding name and by identifier", async () => {
    await load(
      pkg("P1", "Amina", "Hassan", { identifiers: [{ identifier_type: "UNHCR ID", identifier_value: "123-45678901" }] }),
      pkg("P2", "Mohammed", "Abdi"),
      pkg("P3", "Aminata", "Kamau"),
    );
    const names = async (q: string) => (await searchPatients(keys, q)).map((h) => h.patient.name);
    expect(await names("amin")).toEqual(["P1", "P3"]);
    expect(await names("amina")).toEqual(["P1", "P3"]); // exact part ranks first
    expect(await names("hassan amina")).toEqual(["P1"]);
    expect(await names("Muhamad")).toEqual(["P2"]); // phonetic
    expect(await names("123-45678901")).toEqual(["P1"]);
    expect(await names("12345678")).toEqual(["P1"]); // identifier prefix
    expect(await names("P2")).toEqual([]); // too short for an identifier, no name
    expect(await names("zzz")).toEqual([]);
  });

  it("scores possible duplicates like the server (MIN_SCORE 4)", () => {
    const c = fromPackage("k", pkg("P1", "Amina", "Hassan"));
    const base = { first_name: "Amina", last_name: "Hassan", sex: "Female", dob: "1990-05-01", dob_estimated: false, residence: "Village 3", identifiers: [] };
    expect(scoreCandidate(base, c)).toEqual({
      score: 3 + 3 + 1 + 3 + 2,
      reasons: ["same first name", "same last name", "same sex", "same date of birth", "same residence"],
    });
    expect(scoreCandidate({ ...base, first_name: "Hassan", last_name: "Amina" }, c)!.reasons[0]).toBe("same first and last name swapped");
    expect(scoreCandidate({ ...base, first_name: "Aminah", sex: null, dob: null, residence: null }, c)).toEqual({ score: 5, reasons: ["similar first name", "same last name"] });
    // different sex and a DOB far away: below the threshold
    expect(scoreCandidate({ ...base, last_name: "Otieno", sex: "Male", dob: "1960-01-01", residence: null }, c)).toBeNull();
    // estimated DOB within 2 years
    expect(scoreCandidate({ ...base, dob: "1991-08-01", dob_estimated: true, residence: null }, c)!.reasons).toContain("date of birth within 2 years (estimated)");
  });

  it("warns about similar patients and about a reused identifier before an offline registration", async () => {
    await load(pkg("P1", "Amina", "Hassan", { identifiers: [{ identifier_type: "UNHCR ID", identifier_value: "123-45678901" }] }), pkg("P2", "John", "Otieno", { sex: "Male" }));
    const similar = await findSimilar(keys, { first_name: "Amena", last_name: "Hasan", sex: "Female", dob: "1990-05-01", dob_estimated: false, residence: null, identifiers: [] });
    expect(similar.map((s) => s.patient.name)).toEqual(["P1"]);
    expect(similar[0]!.sameIdentifier).toBe(false);
    const byId = await findSimilar(keys, { first_name: "Zed", last_name: "Q", sex: "Male", dob: null, dob_estimated: false, residence: null, identifiers: [{ identifier_type: "UNHCR ID", identifier_value: "123 45678901" }] });
    expect(byId.map((s) => [s.patient.name, s.sameIdentifier])).toEqual([["P1", true]]);
    expect(await findSimilar(keys, { first_name: "Peter", last_name: "Kamau", sex: "Male", dob: null, dob_estimated: false, residence: null, identifiers: [] })).toEqual([]);
  });

  it("validates registration: identifier patterns, primary identifier or walk-in, DOB or age", () => {
    const form = { first_name: "A", last_name: "B", sex: "Female", dob_mode: "exact" as const, dob: "1990-01-01", is_walk_in: false, identifiers: [{ identifier_type: "UNHCR ID", identifier_value: "123-45678901" }] };
    expect(validateRegister(form, REF)).toEqual({});
    expect(validateRegister({ ...form, identifiers: [{ identifier_type: "UNHCR ID", identifier_value: "12345" }] }, REF)).toHaveProperty("identifier_0", "12345 is not a valid UNHCR ID.");
    expect(validateRegister({ ...form, identifiers: [{ identifier_type: "National ID", identifier_value: "999" }] }, REF)).toHaveProperty("identifiers");
    expect(validateRegister({ ...form, identifiers: [], is_walk_in: true }, REF)).toEqual({});
    expect(validateRegister({ ...form, dob_mode: "estimated", age_years: "", age_months: "" }, REF)).toHaveProperty("age_years");
    expect(validateRegister({ ...form, dob_mode: "estimated", age_years: "0", age_months: "13" }, REF)).toHaveProperty("age_months");
    expect(validateRegister({ ...form, dob: "2999-01-01" }, REF)).toHaveProperty("dob");
  });

  it("estimates the DOB from an age, clamped at month end", () => {
    expect(estimateDob("2026-11-20", 30, 0)).toBe("1996-11-20");
    expect(estimateDob("2026-03-31", 0, 1)).toBe("2026-02-28");
    expect(estimateDob("2026-01-15", 1, 2)).toBe("2024-11-15");
  });

  it("registers offline: an encrypted outbox record plus a local patient that search finds", async () => {
    const p = await registerPatient(keys, { session: "S", deviceId: "D" }, {
      first_name: "Fatuma",
      last_name: "Ali",
      sex: "Female",
      dob_mode: "estimated",
      age_years: "25",
      is_walk_in: true,
      identifiers: [],
    });
    expect(p.dob_estimated).toBe(1);
    const [row] = await allRows();
    expect(row!.type).toBe("patient.register");
    const secret = await readSecret(keys, row!);
    expect(secret.envelope.client_uuid).toBe(p.client_uuid);
    expect(secret.envelope.payload).toMatchObject({ first_name: "Fatuma", dob: null, estimated_age_years: 25, is_walk_in: 1 });
    expect((await searchPatients(keys, "fatuma")).map((h) => h.patient.key)).toEqual([p.key]);
  });

  it("edits demographics with base_modified and base_values and applies them locally", async () => {
    const [p] = await load(pkg("P1", "Amina", "Hassan"));
    const updated = await updatePatient(keys, { session: "S", deviceId: "D" }, p!, { mobile: "0712345678" }, [{ identifier_type: "National ID", identifier_value: "1234" }]);
    expect(updated.mobile).toBe("0712345678");
    expect(updated.pending_updates).toBe(1);
    const secret = await readSecret(keys, (await allRows())[0]!);
    expect(secret.envelope.payload).toEqual({
      patient: "P1",
      base_modified: "2026-11-10 08:01:02.123456",
      changes: { mobile: "0712345678" },
      base_values: { mobile: null },
      add_identifiers: [{ identifier_type: "National ID", identifier_value: "1234" }],
    });
    expect((await getPatient(keys, p!.key))!.identifiers).toHaveLength(1);
    await expect(updatePatient(keys, { session: "S", deviceId: "D" }, p!, {}, [])).rejects.toThrow(/Nothing/);
  });
});
