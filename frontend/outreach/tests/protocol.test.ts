import fixtures from "./fixtures/protocol.json";
import {
  buildCommunityActivity,
  buildEnvelope,
  buildPatientRegister,
  buildPatientUpdate,
  type VisitCapturePayload,
} from "../src/lib/protocol";
import { buildVisitCapture, newDraft, type VisitDraft } from "../src/lib/visit";

/** The structure of a JSON value: key names (in order) and value kinds, recursively. */
function shape(v: unknown): unknown {
  if (Array.isArray(v)) return v.length ? [shape(v[0])] : [];
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, shape(x)]));
  return v === null ? "null" : typeof v;
}
/** Like shape(), but a null in the fixture accepts any value (the example just left it empty). */
function keysOnly(v: unknown): unknown {
  if (Array.isArray(v)) return v.length ? [keysOnly(v[0])] : [];
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, keysOnly(x)]));
  return "*";
}

function fullDraft(): VisitDraft {
  const d = newDraft({ key: "k", name: "KLB-PAT-2026-00012", client_uuid: null, display: "Amina Hassan" }, "Outpatient");
  d.vitals = {
    triage_level: "Non-urgent",
    chief_complaint: "Fever",
    temperature: "37.2",
    pulse: "88",
    respiratory_rate: "18",
    bp_systolic: "120",
    bp_diastolic: "80",
    weight: "60.5",
    height: "1.62",
    spo2: "98",
  };
  d.consultation = {
    presenting_complaint: "Fever 3 days",
    history: "h",
    examination_findings: "e",
    management_plan: "m",
    no_diagnosis_reason: "",
    diagnoses: [{ code: "CV-1F40", code_value: "1F40", title: "Malaria", case_type: "New", is_primary: true }],
  };
  d.labs = [{ line_id: "l1", template: "Malaria Rapid Diagnostic Test", poc: true, result: "Positive - P. falciparum", resulted_at: "2026-11-20 10:55:00" }];
  d.prescriptions = [
    {
      line_id: "p1",
      medication: "Artemether/Lumefantrine",
      label: "AL",
      dosage: "BD",
      period: "3 Day",
      dosage_form: "Tablet",
      comment: "",
      dispensed: true,
      quantity: "6",
      dispensed_at: "2026-11-20 11:05:00",
    },
  ];
  return d;
}

describe("payloads match docs/OUTREACH_PROTOCOL.md exactly", () => {
  it("envelope", () => {
    const env = buildEnvelope("community.activity", buildCommunityActivity({ activity_type: "Health talk", activity_date: "2026-11-20" }), {
      session: "KLB-OUT-2026-00001",
      device_id: "a3b2c4d5-0000",
      captured_at: "2026-11-20 10:42:00",
      depends_on: ["x"],
    });
    expect(Object.keys(env)).toEqual(Object.keys(fixtures.envelope));
    expect(env.client_uuid).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(env.captured_at).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });

  it("patient.register (exact DOB, and estimated age)", () => {
    const exact = buildPatientRegister({
      first_name: " Amina ",
      middle_name: "",
      last_name: "Hassan",
      sex: "Female",
      dob_mode: "exact",
      dob: "1990-05-01",
      age_years: "30",
      residence: "Village 3",
      is_walk_in: false,
      identifiers: [{ identifier_type: "UNHCR ID", identifier_value: " 123-45 " }, { identifier_type: "", identifier_value: "" }],
    });
    expect(shape(exact)).toEqual(shape(fixtures["patient.register"]));
    expect(exact).toEqual({
      first_name: "Amina",
      middle_name: null,
      last_name: "Hassan",
      sex: "Female",
      dob: "1990-05-01",
      estimated_age_years: null,
      estimated_age_months: null,
      mobile: null,
      residence: "Village 3",
      allergies: null,
      is_walk_in: 0,
      identifiers: [{ identifier_type: "UNHCR ID", identifier_value: "123-45" }],
    });
    const est = buildPatientRegister({ first_name: "A", last_name: "B", sex: "Male", dob_mode: "estimated", dob: "1990-01-01", age_years: "4", age_months: "6", is_walk_in: true, identifiers: [] });
    expect(Object.keys(est)).toEqual(Object.keys(fixtures["patient.register"]));
    expect([est.dob, est.estimated_age_years, est.estimated_age_months, est.is_walk_in]).toEqual([null, 4, 6, 1]);
  });

  it("patient.update keeps only the allowed change keys", () => {
    const p = buildPatientUpdate(
      "KLB-PAT-2026-00012",
      "2026-11-10 08:01:02.123456",
      { mobile: "0712000000", residence: "R", ...({ owner: "x" } as object) },
      [{ identifier_type: "Alien ID", identifier_value: "A1" }],
    );
    expect(shape(p)).toEqual(shape(fixtures["patient.update"]));
    expect(p.changes).toEqual({ mobile: "0712000000", residence: "R" });
  });

  it("visit.capture with every section", () => {
    const p = buildVisitCapture(fullDraft());
    expect(keysOnly(p)).toEqual(keysOnly(fixtures["visit.capture"]));
    expect(p.vitals).toEqual({ ...fixtures["visit.capture"].vitals, chief_complaint: "Fever" });
    expect(p.lab_requests[0]).toEqual(fixtures["visit.capture"].lab_requests[0]);
    expect(p.prescriptions[0]!.dispensed).toEqual({ quantity: 6, dispensed_at: "2026-11-20 11:05:00" });
    expect(p.consultation.diagnoses).toEqual([{ code: "CV-1F40", case_type: "New", is_primary: 1 }]);
  });

  it("visit.capture minimal: no vitals key, lab to the facility, prescription to the pharmacy, a new patient by reference", () => {
    const d = newDraft({ key: "k", name: null, client_uuid: "reg-uuid", display: "X" }, "Outpatient");
    d.consultation.no_diagnosis_reason = "Review";
    d.labs = [{ line_id: "l1", template: "Full Haemogram", poc: false, result: "", resulted_at: null }];
    d.prescriptions = [{ ...fullDraft().prescriptions[0]!, dispensed: false }];
    const p: VisitCapturePayload = buildVisitCapture(d);
    expect("vitals" in p).toBe(false);
    expect(Object.keys(p)).toEqual(Object.keys(fixtures["visit.capture"]).filter((k) => k !== "vitals"));
    expect([p.patient, p.patient_ref]).toEqual([null, "reg-uuid"]);
    expect(p.lab_requests).toEqual([{ line_id: "l1", observation_template: "Full Haemogram" }]);
    expect("dispensed" in p.prescriptions[0]!).toBe(false);
    expect(p.consultation.no_diagnosis_reason).toBe("Review");
    expect(p.outcome).toBe("Discharged home");
  });

  it("community.activity computes the total", () => {
    const a = buildCommunityActivity({ activity_type: "Health talk", activity_date: "2026-11-20", location: "Village 3 market", topic: "Hygiene", participants_male: "12", participants_female: 25 });
    expect(shape(a)).toEqual(shape(fixtures["community.activity"]));
    expect(a.participants_total).toBe(37);
  });
});
