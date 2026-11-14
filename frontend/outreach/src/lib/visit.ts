/**
 * Visit capture (OUT-11..13): a draft kept encrypted on the device while the stepper is in use, and the
 * `visit.capture` payload built from it when the user finishes.
 */
import { decryptJson, encryptJson, type Keys } from "./crypto";
import { aad, db } from "./db";
import type { IcdEntry } from "./icd";
import type {
  ConsultationPayload,
  LabRequestPayload,
  PrescriptionPayload,
  ReferenceData,
  VisitCapturePayload,
  VitalsPayload,
} from "./protocol";
import { siteNow } from "./time";
import { VITAL_SPECS, parseNumber, validateVitals, vitalsEmpty, type VitalsInput } from "./vitals";

export const STEPS = ["Vitals", "Consultation", "Labs", "Prescriptions", "Outcome", "Review"] as const;

export interface DraftPatient {
  key: string;
  name: string | null;
  client_uuid: string | null;
  display: string;
}

export interface DraftDiagnosis {
  code: string; // Code Value name (payload)
  code_value: string;
  title: string;
  case_type: "New" | "Follow-up";
  is_primary: boolean;
}

export interface DraftLab {
  line_id: string;
  template: string;
  poc: boolean;
  result: string;
  resulted_at: string | null;
}

export interface DraftPrescription {
  line_id: string;
  medication: string;
  label: string;
  dosage: string;
  period: string;
  dosage_form: string;
  comment: string;
  dispensed: boolean;
  quantity: string;
  dispensed_at: string | null;
}

export interface VisitDraft {
  id: string;
  started_at: string; // site time; becomes captured_at (the clinical time)
  updated_at: number;
  step: number;
  patient: DraftPatient;
  department: string;
  vitals: VitalsInput;
  consultation: {
    presenting_complaint: string;
    history: string;
    examination_findings: string;
    management_plan: string;
    no_diagnosis_reason: string;
    diagnoses: DraftDiagnosis[];
  };
  labs: DraftLab[];
  prescriptions: DraftPrescription[];
  outcome: string;
}

export function newDraft(patient: DraftPatient, department: string, now: number = Date.now()): VisitDraft {
  return {
    id: crypto.randomUUID(),
    started_at: siteNow(now),
    updated_at: now,
    step: 0,
    patient,
    department,
    vitals: {},
    consultation: {
      presenting_complaint: "",
      history: "",
      examination_findings: "",
      management_plan: "",
      no_diagnosis_reason: "",
      diagnoses: [],
    },
    labs: [],
    prescriptions: [],
    outcome: "Discharged home",
  };
}

export function diagnosisFromIcd(e: IcdEntry, isFirst: boolean): DraftDiagnosis {
  return { code: e.name, code_value: e.code, title: e.title, case_type: "New", is_primary: isFirst };
}

// ----- validation -------------------------------------------------------------------------------

export type StepErrors = Record<string, string>;

export function validateConsultation(c: VisitDraft["consultation"]): StepErrors {
  const errors: StepErrors = {};
  if (c.diagnoses.length) {
    const primaries = c.diagnoses.filter((d) => d.is_primary).length;
    if (primaries !== 1) errors.diagnoses = "Mark exactly one diagnosis as primary.";
    const codes = new Set<string>();
    for (const d of c.diagnoses) {
      if (codes.has(d.code)) errors.diagnoses = `${d.code_value} is entered twice.`;
      codes.add(d.code);
    }
  } else if (!c.no_diagnosis_reason.trim()) {
    errors.no_diagnosis_reason = "Add a diagnosis, or give the reason there is none.";
  }
  return errors;
}

export function validateLabs(labs: DraftLab[], ref: Pick<ReferenceData, "lab_templates">): StepErrors {
  const errors: StepErrors = {};
  const seen = new Set<string>();
  for (const l of labs) {
    const t = ref.lab_templates.find((x) => x.name === l.template);
    if (!t) errors[l.line_id] = `${l.template} is not in the package.`;
    else if (l.result && !t.point_of_care) errors[l.line_id] = `${l.template} is not a point-of-care test.`;
    else if (l.result && t.options?.length && !t.options.includes(l.result)) {
      errors[l.line_id] = `Choose one of the listed results for ${l.template}.`;
    }
    if (seen.has(l.template)) errors[l.line_id] = `${l.template} is requested twice.`;
    seen.add(l.template);
  }
  return errors;
}

export function validatePrescriptions(rx: DraftPrescription[]): StepErrors {
  const errors: StepErrors = {};
  for (const p of rx) {
    if (!p.medication) errors[p.line_id] = "Choose a medication.";
    else if (p.dispensed) {
      const q = parseNumber(p.quantity);
      if (typeof q !== "number" || q <= 0) errors[p.line_id] = `Enter the quantity dispensed for ${p.label}.`;
    }
  }
  return errors;
}

export function validateOutcome(outcome: string, ref: Pick<ReferenceData, "outcomes">): StepErrors {
  const allowed = ref.outcomes?.length ? ref.outcomes : ["Discharged home", "Died"];
  return allowed.includes(outcome) ? {} : { outcome: "Choose the outcome." };
}

export function validateStep(draft: VisitDraft, step: number, ref: ReferenceData): StepErrors {
  switch (step) {
    case 0:
      return validateVitals(draft.vitals);
    case 1:
      return validateConsultation(draft.consultation);
    case 2:
      return validateLabs(draft.labs, ref);
    case 3:
      return validatePrescriptions(draft.prescriptions);
    case 4:
      return validateOutcome(draft.outcome, ref);
    default:
      return {};
  }
}

/** The first step with errors, or -1 when the draft can be finished. */
export function firstInvalidStep(draft: VisitDraft, ref: ReferenceData): number {
  for (let s = 0; s < 5; s++) if (Object.keys(validateStep(draft, s, ref)).length) return s;
  if (!draft.department) return 0;
  return -1;
}

// ----- payload ------------------------------------------------------------------------------------

const text = (v: string | undefined | null): string | null => {
  const t = (v ?? "").trim();
  return t ? t : null;
};

export function buildVitals(v: VitalsInput): VitalsPayload | undefined {
  if (vitalsEmpty(v)) return undefined;
  const values = Object.fromEntries(
    VITAL_SPECS.map((s) => {
      const n = parseNumber(v[s.field]);
      return [s.field, typeof n === "number" ? n : null];
    }),
  ) as Record<(typeof VITAL_SPECS)[number]["field"], number | null>;
  return {
    triage_level: (v.triage_level ?? "").trim(),
    chief_complaint: (v.chief_complaint ?? "").trim(),
    temperature: values.temperature,
    pulse: values.pulse,
    respiratory_rate: values.respiratory_rate,
    bp_systolic: values.bp_systolic,
    bp_diastolic: values.bp_diastolic,
    weight: values.weight,
    height: values.height,
    spo2: values.spo2,
  };
}

export function buildVisitCapture(draft: VisitDraft): VisitCapturePayload {
  const c = draft.consultation;
  const consultation: ConsultationPayload = {
    presenting_complaint: text(c.presenting_complaint),
    history: text(c.history),
    examination_findings: text(c.examination_findings),
    management_plan: text(c.management_plan),
    no_diagnosis_reason: c.diagnoses.length ? null : text(c.no_diagnosis_reason),
    diagnoses: c.diagnoses.map((d) => ({ code: d.code, case_type: d.case_type, is_primary: d.is_primary ? 1 : 0 })),
  };
  const lab_requests: LabRequestPayload[] = draft.labs.map((l) => {
    const line: LabRequestPayload = { line_id: l.line_id, observation_template: l.template };
    if (l.poc && l.result.trim()) {
      line.poc_result = { result: l.result.trim(), resulted_at: l.resulted_at ?? draft.started_at };
    }
    return line;
  });
  const prescriptions: PrescriptionPayload[] = draft.prescriptions.map((p) => {
    const line: PrescriptionPayload = {
      line_id: p.line_id,
      medication: p.medication,
      dosage: text(p.dosage),
      period: text(p.period),
      dosage_form: text(p.dosage_form),
      comment: text(p.comment),
    };
    if (p.dispensed) {
      line.dispensed = {
        quantity: Number(parseNumber(p.quantity) ?? 0),
        dispensed_at: p.dispensed_at ?? draft.started_at,
      };
    }
    return line;
  });
  const isLocal = Boolean(draft.patient.client_uuid);
  const vitals = buildVitals(draft.vitals);
  // key order as in the protocol: patient, patient_ref, department, vitals, consultation, ...
  return {
    patient: isLocal ? null : draft.patient.name,
    patient_ref: isLocal ? draft.patient.client_uuid : null,
    department: draft.department,
    ...(vitals ? { vitals } : {}),
    consultation,
    lab_requests,
    prescriptions,
    outcome: draft.outcome || "Discharged home",
  };
}

// ----- draft storage ----------------------------------------------------------------------------

export async function saveDraft(keys: Keys, draft: VisitDraft, now: number = Date.now()): Promise<void> {
  const updated = { ...draft, updated_at: now };
  await (await db()).put("drafts", { id: draft.id, updated_at: now, enc: await encryptJson(keys, updated, aad("drafts", draft.id)) });
}

export async function loadDraft(keys: Keys, id: string): Promise<VisitDraft | null> {
  const row = await (await db()).get("drafts", id);
  return row ? decryptJson<VisitDraft>(keys, row.enc, aad("drafts", id)) : null;
}

export async function listDrafts(keys: Keys): Promise<VisitDraft[]> {
  const rows = await (await db()).getAll("drafts");
  const out: VisitDraft[] = [];
  for (const r of rows) out.push(await decryptJson<VisitDraft>(keys, r.enc, aad("drafts", r.id)));
  return out.sort((a, b) => b.updated_at - a.updated_at);
}

export async function countDrafts(): Promise<number> {
  return (await db()).count("drafts");
}

export async function deleteDraft(id: string): Promise<void> {
  await (await db()).delete("drafts", id);
}
