import { allRows, readSecret } from "../src/lib/outbox";
import { fromPackage, getPatient, patientKey, putPatient } from "../src/lib/patients";
import type { ReferenceData } from "../src/lib/protocol";
import { buildIcdIndex } from "../src/lib/icd";
import type { LoadedReference } from "../src/lib/reference";
import { finishVisit } from "../src/lib/records";
import { countDrafts, firstInvalidStep, listDrafts, loadDraft, newDraft, saveDraft, validateConsultation, validateLabs, validatePrescriptions } from "../src/lib/visit";
import { validateVitals } from "../src/lib/vitals";
import { db } from "../src/lib/db";
import { freshDevice, testKeys } from "./helpers";

const DATA: ReferenceData = {
  icd11: [["CV-1F40", "1F40", "Malaria due to Plasmodium falciparum", 1]],
  medications: [],
  dosages: ["BD"],
  periods: ["3 Day"],
  dosage_forms: ["Tablet"],
  lab_templates: [
    { name: "Malaria Rapid Diagnostic Test", abbr: "MRDT", point_of_care: 1, data_type: "Select", options: ["Negative", "Positive - P. falciparum"], unit: null },
    { name: "Full Haemogram", abbr: "FHG", point_of_care: 0, data_type: null, options: null, unit: null },
  ],
  departments: ["Outpatient"],
  identifier_types: [],
  residences: [],
  triage_levels: ["Emergency", "Priority", "Non-urgent"],
  community_activity_types: ["Health talk", "Home visit"],
  outcomes: ["Discharged home", "Died"],
  genders: ["Female", "Male"],
};
const REF: LoadedReference = { ...DATA, icdIndex: buildIcdIndex(DATA.icd11), icdByName: new Map() };

describe("vitals plausibility (same ranges as visit/events.py)", () => {
  it("accepts an empty step and plausible values", () => {
    expect(validateVitals({})).toEqual({});
    expect(validateVitals({ triage_level: "Priority", chief_complaint: "Fever", temperature: "38.5", spo2: "97", bp_systolic: "120", bp_diastolic: "80", weight: "0.3", height: "2.5" })).toEqual({});
  });
  it("refuses implausible values and incomplete steps", () => {
    const base = { triage_level: "Priority", chief_complaint: "Fever" };
    expect(validateVitals({ ...base, temperature: "24.9" })).toHaveProperty("temperature");
    expect(validateVitals({ ...base, temperature: "45.1" })).toHaveProperty("temperature");
    expect(validateVitals({ ...base, spo2: "49" })).toHaveProperty("spo2");
    expect(validateVitals({ ...base, spo2: "101" })).toHaveProperty("spo2");
    expect(validateVitals({ ...base, weight: "351" })).toHaveProperty("weight");
    expect(validateVitals({ ...base, height: "165" }).height).toMatch(/metres/);
    expect(validateVitals({ ...base, pulse: "72.5" }).pulse).toMatch(/whole/);
    expect(validateVitals({ ...base, bp_systolic: "120" }).bp).toMatch(/both/);
    expect(validateVitals({ ...base, bp_systolic: "80", bp_diastolic: "80" }).bp).toMatch(/higher/);
    expect(validateVitals({ temperature: "37" })).toMatchObject({ triage_level: expect.any(String), chief_complaint: expect.any(String) });
    expect(validateVitals(base)).toHaveProperty("_form");
    expect(validateVitals({ ...base, temperature: "abc" }).temperature).toMatch(/number/);
  });
});

describe("visit steps", () => {
  it("needs a diagnosis with exactly one primary, or a reason", () => {
    const c = newDraft({ key: "k", name: "P", client_uuid: null, display: "X" }, "Outpatient").consultation;
    expect(validateConsultation(c)).toHaveProperty("no_diagnosis_reason");
    expect(validateConsultation({ ...c, no_diagnosis_reason: "Review" })).toEqual({});
    const dx = { code: "CV-1F40", code_value: "1F40", title: "M", case_type: "New" as const, is_primary: false };
    expect(validateConsultation({ ...c, diagnoses: [dx] })).toHaveProperty("diagnoses");
    expect(validateConsultation({ ...c, diagnoses: [{ ...dx, is_primary: true }] })).toEqual({});
  });
  it("allows results only for point-of-care tests, from their options", () => {
    const lab = { line_id: "l1", template: "Full Haemogram", poc: false, result: "12", resulted_at: null };
    expect(validateLabs([lab], DATA)).toHaveProperty("l1");
    expect(validateLabs([{ ...lab, template: "Malaria Rapid Diagnostic Test", poc: true, result: "Maybe" }], DATA)).toHaveProperty("l1");
    expect(validateLabs([{ ...lab, template: "Malaria Rapid Diagnostic Test", poc: true, result: "Negative" }], DATA)).toEqual({});
  });
  it("needs a quantity for a dispensed prescription", () => {
    const rx = { line_id: "p1", medication: "AL", label: "AL", dosage: "BD", period: "3 Day", dosage_form: "Tablet", comment: "", dispensed: true, quantity: "", dispensed_at: null };
    expect(validatePrescriptions([rx])).toHaveProperty("p1");
    expect(validatePrescriptions([{ ...rx, quantity: "6" }])).toEqual({});
  });
});

describe("drafts and finishing a visit", () => {
  beforeEach(freshDevice);

  it("keeps the draft encrypted until finished, then queues visit.capture depending on the registration", async () => {
    const keys = await testKeys();
    const key = await patientKey(keys, { name: null, client_uuid: "reg-1" });
    await putPatient(keys, { ...fromPackage(key, { name: "", modified: "", first_name: "Fatuma", middle_name: null, last_name: "Ali", sex: "Female", dob: null, dob_estimated: 1, mobile: null, residence: null, allergies: null, identifiers: [], recent_visits: [], active_prescriptions: [] }), name: null, client_uuid: "reg-1" });
    const draft = newDraft({ key, name: null, client_uuid: "reg-1", display: "Fatuma Ali" }, "Outpatient");
    draft.consultation.no_diagnosis_reason = "Counselling";
    await saveDraft(keys, draft);
    const raw = JSON.stringify(await (await db()).getAll("drafts"), (_k, v) => (v instanceof Uint8Array ? new TextDecoder().decode(v) : v));
    expect(raw).not.toContain("Fatuma");
    expect((await listDrafts(keys)).map((d) => d.id)).toEqual([draft.id]);
    expect(firstInvalidStep((await loadDraft(keys, draft.id))!, REF)).toBe(-1);

    const uuid = await finishVisit(keys, { session: "S", deviceId: "D" }, draft, REF);
    expect(await countDrafts()).toBe(0);
    const [row] = await allRows();
    expect(row!.client_uuid).toBe(uuid);
    const env = (await readSecret(keys, row!)).envelope;
    expect(env.type).toBe("visit.capture");
    expect(env.depends_on).toEqual(["reg-1"]);
    expect(env.captured_at).toBe(draft.started_at);
    expect((await getPatient(keys, key))!.outreach_visits.map((v) => v.client_uuid)).toEqual([uuid]);
  });

  it("refuses to finish an invalid draft", async () => {
    const keys = await testKeys();
    const draft = newDraft({ key: "k", name: "P1", client_uuid: null, display: "X" }, "Outpatient");
    await expect(finishVisit(keys, { session: "S", deviceId: "D" }, draft, REF)).rejects.toThrow(/attention/);
    expect(await allRows()).toEqual([]);
  });
});
