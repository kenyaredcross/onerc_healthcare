/**
 * Wire types for the outreach sync protocol (apps/onerc_healthcare/docs/OUTREACH_PROTOCOL.md).
 * Every payload the app sends or receives has a type here, and every record the app sends is built by
 * one of the builders below, so the shapes are checked against the protocol fixtures in tests.
 */

// ---------------------------------------------------------------------------------------------
// Constants (mirror onerc_healthcare/constants.py, Phase 4 block)
// ---------------------------------------------------------------------------------------------

export const AUTH_SCHEME = "krcs-outreach";
export const METHOD_PREFIX = "/api/method/onerc_healthcare.outreach.";

export const RECORD_PATIENT_REGISTER = "patient.register";
export const RECORD_PATIENT_UPDATE = "patient.update";
export const RECORD_VISIT_CAPTURE = "visit.capture";
export const RECORD_COMMUNITY_ACTIVITY = "community.activity";
export type RecordType =
  | typeof RECORD_PATIENT_REGISTER
  | typeof RECORD_PATIENT_UPDATE
  | typeof RECORD_VISIT_CAPTURE
  | typeof RECORD_COMMUNITY_ACTIVITY;

export const RESULTS = ["accepted", "duplicate", "failed", "held", "conflict"] as const;
export type SyncResultValue = (typeof RESULTS)[number];

/** Results after which the device may purge the record (protocol §4 "Final states"). */
export const FINAL_RESULTS: readonly SyncResultValue[] = ["accepted", "duplicate"];

export const DEFAULT_SETTINGS: DeviceSettings = {
  offline_session_days: 7,
  purge_after_days: 7,
  pin_min_length: 6,
  max_pin_attempts: 5,
  max_batch: 50,
  summary_visits: 3,
};

/** Keys a `patient.update` may change (protocol §4). */
export const PATIENT_UPDATE_KEYS = [
  "first_name",
  "middle_name",
  "last_name",
  "sex",
  "dob",
  "estimated_age_years",
  "estimated_age_months",
  "mobile",
  "residence",
  "allergies",
] as const;
export type PatientUpdateKey = (typeof PATIENT_UPDATE_KEYS)[number];

// ---------------------------------------------------------------------------------------------
// §2 Devices and sessions
// ---------------------------------------------------------------------------------------------

export interface OutreachSessionSummary {
  name: string;
  title: string;
  facility: string;
  facility_name: string;
  start_date: string;
  end_date: string;
  catchment_area: string;
  status: "Planned" | "Active" | "Returned" | string;
  departments: string[];
}

export interface EnrolArgs {
  session: string;
  device_id: string;
  device_label: string;
  data_key: string; // base64 raw 256-bit AES key (escrow)
}

export interface DeviceSettings {
  offline_session_days: number;
  purge_after_days: number;
  pin_min_length: number;
  max_pin_attempts: number;
  max_batch: number;
  summary_visits: number;
}

export interface EnrolResponse {
  grant: string;
  secret: string;
  expires_at: string; // "YYYY-MM-DD HH:mm:ss", site time
  user: { name: string; full_name: string };
  settings: DeviceSettings;
}

export interface RecoverKeyResponse {
  data_key: string;
}

// ---------------------------------------------------------------------------------------------
// §3 Package
// ---------------------------------------------------------------------------------------------

/** `[name, code_value, display, is_leaf]` */
export type IcdRow = [string, string, string, number];

export interface ReferenceMedication {
  name: string;
  generic_name: string | null;
  strength: string | null;
  dosage_form: string | null;
  default_dosage: string | null;
  default_period: string | null;
}

export interface ReferenceLabTemplate {
  name: string;
  abbr: string | null;
  point_of_care: number;
  data_type: string | null;
  options: string[] | null;
  unit: string | null;
}

export interface ReferenceIdentifierType {
  name: string;
  is_primary: number;
  pattern: string | null;
  facility_generated: number;
}

export interface ReferenceResidence {
  name: string;
  location_name: string;
  parent: string | null;
  is_group: number;
  level: string | null;
}

export interface ReferenceData {
  icd11: IcdRow[];
  medications: ReferenceMedication[];
  dosages: string[];
  periods: string[];
  dosage_forms: string[];
  lab_templates: ReferenceLabTemplate[];
  departments: string[];
  identifier_types: ReferenceIdentifierType[];
  residences: ReferenceResidence[];
  triage_levels: string[];
  community_activity_types: string[];
  outcomes: string[];
  genders: string[];
}

export type ReferenceResponse =
  | { version: string; unchanged: true }
  | { version: string; unchanged: false; data: ReferenceData };

export interface Identifier {
  identifier_type: string;
  identifier_value: string;
}

export interface PackageDiagnosis {
  code: string;
  title: string;
  case_type: string;
}

export interface PackageVisit {
  date: string;
  facility: string;
  department: string;
  diagnoses: PackageDiagnosis[];
}

export interface PackagePrescription {
  medication: string;
  dosage: string | null;
  period: string | null;
  date: string;
}

export interface PackagePatient {
  name: string;
  modified: string;
  first_name: string;
  middle_name: string | null;
  last_name: string | null;
  sex: string | null;
  dob: string | null;
  dob_estimated: number;
  mobile: string | null;
  residence: string | null;
  allergies: string | null;
  identifiers: Identifier[];
  recent_visits: PackageVisit[];
  active_prescriptions: PackagePrescription[];
}

export interface PatientsPage {
  page: number;
  has_more: boolean;
  total: number;
  patients: PackagePatient[];
}

// ---------------------------------------------------------------------------------------------
// §4 Records
// ---------------------------------------------------------------------------------------------

export interface PatientRegisterPayload {
  first_name: string;
  middle_name: string | null;
  last_name: string | null;
  sex: string;
  dob: string | null;
  estimated_age_years: number | null;
  estimated_age_months: number | null;
  mobile: string | null;
  residence: string | null;
  allergies: string | null;
  is_walk_in: 0 | 1;
  identifiers: Identifier[];
}

export type PatientChanges = Partial<Record<PatientUpdateKey, string | number | null>>;

export interface PatientUpdatePayload {
  patient: string;
  base_modified: string;
  changes: PatientChanges;
  add_identifiers: Identifier[];
}

export interface VitalsPayload {
  triage_level: string;
  chief_complaint: string;
  temperature: number | null;
  pulse: number | null;
  respiratory_rate: number | null;
  bp_systolic: number | null;
  bp_diastolic: number | null;
  weight: number | null;
  height: number | null;
  spo2: number | null;
}

export interface DiagnosisPayload {
  code: string; // Code Value name
  case_type: "New" | "Follow-up";
  is_primary: 0 | 1;
}

export interface ConsultationPayload {
  presenting_complaint: string | null;
  history: string | null;
  examination_findings: string | null;
  management_plan: string | null;
  no_diagnosis_reason: string | null;
  diagnoses: DiagnosisPayload[];
}

export interface LabRequestPayload {
  line_id: string;
  observation_template: string;
  poc_result?: { result: string; resulted_at: string };
}

export interface PrescriptionPayload {
  line_id: string;
  medication: string;
  dosage: string | null;
  period: string | null;
  dosage_form: string | null;
  comment: string | null;
  dispensed?: { quantity: number; dispensed_at: string };
}

export interface VisitCapturePayload {
  patient: string | null;
  patient_ref: string | null;
  department: string;
  vitals?: VitalsPayload;
  consultation: ConsultationPayload;
  lab_requests: LabRequestPayload[];
  prescriptions: PrescriptionPayload[];
  outcome: string;
}

export interface CommunityActivityPayload {
  activity_type: string;
  activity_date: string;
  location: string | null;
  topic: string | null;
  participants_male: number;
  participants_female: number;
  participants_total: number;
  notes: string | null;
}

export interface PayloadByType {
  "patient.register": PatientRegisterPayload;
  "patient.update": PatientUpdatePayload;
  "visit.capture": VisitCapturePayload;
  "community.activity": CommunityActivityPayload;
}

export interface Envelope<T extends RecordType = RecordType> {
  client_uuid: string;
  type: T;
  session: string;
  device_id: string;
  captured_at: string;
  depends_on: string[];
  payload: PayloadByType[T];
}

export type AnyEnvelope = { [K in RecordType]: Envelope<K> }[RecordType];

export interface PushArgs {
  session: string;
  device_id: string;
  batch_id: string;
  records: AnyEnvelope[];
}

export interface RecordResult {
  client_uuid: string;
  result: SyncResultValue;
  doctype: string | null;
  name: string | null;
  conflict: string | null;
  error: string | null;
  discarded?: boolean;
}

export interface PushResponse {
  batch: string;
  results: RecordResult[];
}

export interface StatusResponse {
  results: RecordResult[];
}

// ---------------------------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------------------------

export interface EnvelopeContext {
  session: string;
  device_id: string;
  captured_at: string;
  client_uuid?: string;
  depends_on?: string[];
}

export function newUuid(): string {
  return crypto.randomUUID();
}

export function buildEnvelope<T extends RecordType>(
  type: T,
  payload: PayloadByType[T],
  ctx: EnvelopeContext,
): Envelope<T> {
  return {
    client_uuid: ctx.client_uuid ?? newUuid(),
    type,
    session: ctx.session,
    device_id: ctx.device_id,
    captured_at: ctx.captured_at,
    depends_on: [...(ctx.depends_on ?? [])],
    payload,
  };
}

const blank = (v: string | null | undefined): string | null => {
  const t = (v ?? "").trim();
  return t ? t : null;
};
const num = (v: number | string | null | undefined): number | null => {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(String(v).replace(",", "."));
  return Number.isFinite(n) ? n : null;
};

export interface RegisterForm {
  first_name: string;
  middle_name?: string | null;
  last_name?: string | null;
  sex: string;
  dob_mode: "exact" | "estimated";
  dob?: string | null;
  age_years?: number | string | null;
  age_months?: number | string | null;
  mobile?: string | null;
  residence?: string | null;
  allergies?: string | null;
  is_walk_in: boolean;
  identifiers: Identifier[];
}

export function buildPatientRegister(form: RegisterForm): PatientRegisterPayload {
  const exact = form.dob_mode === "exact";
  return {
    first_name: form.first_name.trim(),
    middle_name: blank(form.middle_name),
    last_name: blank(form.last_name),
    sex: form.sex,
    dob: exact ? blank(form.dob) : null,
    estimated_age_years: exact ? null : num(form.age_years),
    estimated_age_months: exact ? null : num(form.age_months),
    mobile: blank(form.mobile),
    residence: blank(form.residence),
    allergies: blank(form.allergies),
    is_walk_in: form.is_walk_in ? 1 : 0,
    identifiers: form.identifiers
      .map((i) => ({ identifier_type: i.identifier_type, identifier_value: i.identifier_value.trim() }))
      .filter((i) => i.identifier_type && i.identifier_value),
  };
}

export function buildPatientUpdate(
  patient: string,
  baseModified: string,
  changes: PatientChanges,
  addIdentifiers: Identifier[],
): PatientUpdatePayload {
  const clean: PatientChanges = {};
  for (const key of PATIENT_UPDATE_KEYS) {
    if (key in changes) clean[key] = changes[key] ?? null;
  }
  return {
    patient,
    base_modified: baseModified,
    changes: clean,
    add_identifiers: addIdentifiers
      .map((i) => ({ identifier_type: i.identifier_type, identifier_value: i.identifier_value.trim() }))
      .filter((i) => i.identifier_type && i.identifier_value),
  };
}

export interface CommunityActivityForm {
  activity_type: string;
  activity_date: string;
  location?: string | null;
  topic?: string | null;
  participants_male?: number | string | null;
  participants_female?: number | string | null;
  notes?: string | null;
}

export function buildCommunityActivity(form: CommunityActivityForm): CommunityActivityPayload {
  const male = Math.max(0, Math.trunc(num(form.participants_male) ?? 0));
  const female = Math.max(0, Math.trunc(num(form.participants_female) ?? 0));
  return {
    activity_type: form.activity_type,
    activity_date: form.activity_date,
    location: blank(form.location),
    topic: blank(form.topic),
    participants_male: male,
    participants_female: female,
    participants_total: male + female,
    notes: blank(form.notes),
  };
}
