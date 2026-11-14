import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { IdentifiersEditor } from "../components/Identifiers";
import { ResidencePicker } from "../components/Residence";
import { Alert, Choice, Field, Page, Spinner, TextArea, TextInput, errorText } from "../components/ui";
import { getPatient, type LocalPatient } from "../lib/patients";
import type { Identifier, PatientChanges } from "../lib/protocol";
import { updatePatient, validateIdentifiers } from "../lib/records";
import { getDeviceId, requireKeys } from "../lib/session";
import { siteToday } from "../lib/time";

export interface EditForm {
  first_name: string;
  middle_name: string;
  last_name: string;
  sex: string;
  dob_mode: "keep" | "exact" | "estimated";
  dob: string;
  age_years: string;
  age_months: string;
  mobile: string;
  residence: string;
  allergies: string;
}

const norm = (v: string | null | undefined): string | null => {
  const t = (v ?? "").trim();
  return t ? t : null;
};

export function diffChanges(p: LocalPatient, f: EditForm): PatientChanges {
  const changes: PatientChanges = {};
  for (const k of ["first_name", "middle_name", "last_name", "sex", "mobile", "residence", "allergies"] as const) {
    if (norm(f[k]) !== norm(p[k])) changes[k] = norm(f[k]);
  }
  if (f.dob_mode === "exact" && f.dob && f.dob !== p.dob) changes.dob = f.dob;
  if (f.dob_mode === "estimated") {
    changes.estimated_age_years = Number(f.age_years || 0);
    changes.estimated_age_months = Number(f.age_months || 0);
  }
  return changes;
}

export function editFormOf(p: LocalPatient): EditForm {
  return {
    first_name: p.first_name ?? "",
    middle_name: p.middle_name ?? "",
    last_name: p.last_name ?? "",
    sex: p.sex ?? "",
    dob_mode: "keep",
    dob: p.dob ?? "",
    age_years: "",
    age_months: "",
    mobile: p.mobile ?? "",
    residence: p.residence ?? "",
    allergies: p.allergies ?? "",
  };
}

export default function EditPatientScreen() {
  const { key = "" } = useParams();
  const { profile } = useApp();
  const [patient, setPatient] = useState<LocalPatient | null | undefined>(undefined);

  useEffect(() => {
    void getPatient(requireKeys(), key).then(setPatient);
  }, [key]);

  if (patient === undefined) return <Spinner />;
  if (!patient || !profile) return null;
  return (
    <EditPatientEditor
      title="Edit demographics"
      patient={patient}
      base={patient}
      initialForm={editFormOf(patient)}
      initialIds={[]}
      onSave={async (changes, ids) => {
        await updatePatient(requireKeys(), { session: profile.session, deviceId: await getDeviceId() }, patient, changes, ids);
      }}
    />
  );
}

/**
 * The demographic edit form. `base` is what each field is compared with to find the changes: the patient
 * as shown for a new edit, or the refused record's `base_values` for a correction (F2).
 */
export function EditPatientEditor({
  title,
  patient,
  base,
  initialForm,
  initialIds,
  onSave,
  intro,
  done,
}: {
  title: string;
  patient: LocalPatient;
  base: LocalPatient;
  initialForm: EditForm;
  initialIds: Identifier[];
  onSave: (changes: PatientChanges, ids: Identifier[]) => Promise<void>;
  intro?: ReactNode;
  done?: string;
}) {
  const navigate = useNavigate();
  const { reference, profile, refresh, sync } = useApp();
  const [form, setForm] = useState<EditForm | null>(initialForm);
  const [newIds, setNewIds] = useState<Identifier[]>(initialIds);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!form || !reference || !profile) return null;
  const set = <K extends keyof EditForm>(k: K, v: EditForm[K]) => setForm((f) => (f ? { ...f, [k]: v } : f));

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!form || !patient || !reference || !profile) return;
    const errs: Record<string, string> = validateIdentifiers(newIds, reference);
    if (!form.first_name.trim()) errs.first_name = "Enter the first name.";
    if (form.dob_mode === "exact" && (!form.dob || form.dob > siteToday())) errs.dob = "Enter a valid date of birth.";
    if (form.dob_mode === "estimated" && !(Number(form.age_years) > 0 || Number(form.age_months) > 0)) errs.age_years = "Enter the estimated age.";
    setErrors(errs);
    if (Object.keys(errs).length) return;
    setBusy(true);
    setError(null);
    try {
      await onSave(diffChanges(base, form), newIds);
      await refresh();
      void sync("record");
      navigate(done ?? `/patients/${encodeURIComponent(patient.key)}`, { replace: true });
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Page title={title} back={done ?? `/patients/${encodeURIComponent(patient.key)}`}>
      {intro}
      <Alert>Changes are sent to the facility at sync. If the record was changed there meanwhile, the HIS Officer reviews both versions.</Alert>
      <form onSubmit={submit} className="space-y-4" noValidate>
        <div className="card grid gap-4 sm:grid-cols-3">
          <Field label="First name" error={errors.first_name}>
            <TextInput value={form.first_name} onChange={(e) => set("first_name", e.target.value)} />
          </Field>
          <Field label="Middle name">
            <TextInput value={form.middle_name} onChange={(e) => set("middle_name", e.target.value)} />
          </Field>
          <Field label="Last name">
            <TextInput value={form.last_name} onChange={(e) => set("last_name", e.target.value)} />
          </Field>
        </div>
        <div className="card space-y-4">
          <Field label="Sex">
            <Choice name="Sex" options={reference.genders.length ? reference.genders : ["Female", "Male"]} value={form.sex} onChange={(v) => set("sex", v)} />
          </Field>
          <Field label={`Date of birth (now ${patient.dob ?? "unknown"}${patient.dob_estimated ? ", estimated" : ""})`}>
            <Choice
              name="Date of birth"
              options={["Keep", "Exact date", "Estimated age"]}
              value={form.dob_mode === "keep" ? "Keep" : form.dob_mode === "exact" ? "Exact date" : "Estimated age"}
              onChange={(v) => set("dob_mode", v === "Keep" ? "keep" : v === "Exact date" ? "exact" : "estimated")}
            />
          </Field>
          {form.dob_mode === "exact" ? (
            <Field label="Exact date of birth" error={errors.dob}>
              <TextInput type="date" max={siteToday()} value={form.dob} onChange={(e) => set("dob", e.target.value)} />
            </Field>
          ) : null}
          {form.dob_mode === "estimated" ? (
            <div className="grid grid-cols-2 gap-3">
              <Field label="Age (years)" error={errors.age_years}>
                <TextInput inputMode="numeric" value={form.age_years} onChange={(e) => set("age_years", e.target.value.replace(/\D/g, ""))} />
              </Field>
              <Field label="and months">
                <TextInput inputMode="numeric" value={form.age_months} onChange={(e) => set("age_months", e.target.value.replace(/\D/g, ""))} />
              </Field>
            </div>
          ) : null}
        </div>
        <div className="card space-y-3">
          <Field label="Residence">
            <ResidencePicker residences={reference.residences} value={form.residence} onChange={(v) => set("residence", v)} />
          </Field>
          <Field label="Mobile">
            <TextInput type="tel" value={form.mobile} onChange={(e) => set("mobile", e.target.value)} />
          </Field>
          <Field label="Allergies">
            <TextArea rows={2} value={form.allergies} onChange={(e) => set("allergies", e.target.value)} />
          </Field>
        </div>
        <div className="card space-y-2">
          <h2 className="font-semibold">Add identifiers</h2>
          <IdentifiersEditor types={reference.identifier_types} value={newIds} onChange={setNewIds} errors={errors} />
        </div>
        {error ? <Alert kind="error">{error}</Alert> : null}
        <button type="submit" className="btn-primary w-full" disabled={busy}>
          {busy ? "Saving…" : "Save changes"}
        </button>
      </form>
    </Page>
  );
}
