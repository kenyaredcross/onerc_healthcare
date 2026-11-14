/**
 * Vital-sign plausibility, the same ranges as the server (onerc_healthcare/visit/events.py), so a
 * visit captured offline is not refused at sync for a typing error.
 */
export interface VitalSpec {
  field: VitalField;
  label: string;
  unit: string;
  low: number;
  high: number;
  whole?: boolean;
  example: string;
}

export type VitalField =
  | "temperature"
  | "pulse"
  | "respiratory_rate"
  | "bp_systolic"
  | "bp_diastolic"
  | "weight"
  | "height"
  | "spo2";

export const VITAL_SPECS: VitalSpec[] = [
  { field: "temperature", label: "Temperature", unit: "°C", low: 25, high: 45, example: "36.8" },
  { field: "pulse", label: "Pulse", unit: "beats/min", low: 20, high: 250, whole: true, example: "72" },
  { field: "respiratory_rate", label: "Respiratory rate", unit: "breaths/min", low: 4, high: 100, whole: true, example: "16" },
  { field: "bp_systolic", label: "BP systolic", unit: "mmHg", low: 40, high: 300, whole: true, example: "120" },
  { field: "bp_diastolic", label: "BP diastolic", unit: "mmHg", low: 20, high: 200, whole: true, example: "80" },
  { field: "weight", label: "Weight", unit: "kg", low: 0.3, high: 350, example: "62.5" },
  { field: "height", label: "Height", unit: "m", low: 0.2, high: 2.5, example: "1.65" },
  { field: "spo2", label: "SpO2", unit: "%", low: 50, high: 100, example: "97" },
];

export type VitalsInput = Partial<Record<VitalField, string>> & {
  triage_level?: string;
  chief_complaint?: string;
};

export function parseNumber(raw: string | null | undefined): number | null | undefined {
  const t = (raw ?? "").trim().replace(",", ".");
  if (!t) return undefined; // blank
  const n = Number(t);
  return Number.isFinite(n) ? n : null; // null: not a number
}

/** Errors keyed by field ("_form" for the whole step). Empty object: valid. */
export function validateVitals(v: VitalsInput): Record<string, string> {
  const errors: Record<string, string> = {};
  let anyValue = false;
  for (const spec of VITAL_SPECS) {
    const n = parseNumber(v[spec.field]);
    if (n === undefined) continue;
    anyValue = true;
    if (n === null) {
      errors[spec.field] = `${spec.label} must be a number, e.g. ${spec.example}.`;
    } else if (spec.whole && !Number.isInteger(n)) {
      errors[spec.field] = `${spec.label} must be a whole number.`;
    } else if (n < spec.low || n > spec.high) {
      errors[spec.field] =
        spec.field === "height" && n >= 20 && n <= 250
          ? `Height is recorded in metres, not centimetres: enter ${n} as ${Math.round(n) / 100}.`
          : `${spec.label} of ${n} ${spec.unit} is not plausible. Expected between ${spec.low} and ${spec.high} ${spec.unit}.`;
    }
  }
  const sys = parseNumber(v.bp_systolic);
  const dia = parseNumber(v.bp_diastolic);
  if ((sys === undefined) !== (dia === undefined)) {
    errors.bp = "Enter both systolic and diastolic blood pressure, or neither.";
  } else if (typeof sys === "number" && typeof dia === "number" && sys <= dia) {
    errors.bp = `Systolic blood pressure (${sys}) must be higher than diastolic (${dia}).`;
  }
  const started = anyValue || Boolean(v.triage_level?.trim()) || Boolean(v.chief_complaint?.trim());
  if (started) {
    if (!v.triage_level?.trim()) errors.triage_level = "Choose the triage level.";
    if (!v.chief_complaint?.trim()) errors.chief_complaint = "Enter the chief complaint.";
    if (!anyValue) errors._form = "Enter at least one vital sign, or leave the whole step empty.";
  }
  return errors;
}

/** True when nothing was entered on the vitals step (the payload then has no `vitals`). */
export function vitalsEmpty(v: VitalsInput): boolean {
  return (
    !v.triage_level?.trim() &&
    !v.chief_complaint?.trim() &&
    VITAL_SPECS.every((s) => parseNumber(v[s.field]) === undefined)
  );
}
