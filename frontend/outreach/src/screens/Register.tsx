import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { IdentifiersEditor } from "../components/Identifiers";
import { ResidencePicker } from "../components/Residence";
import { Alert, Choice, Field, Page, TextArea, TextInput, errorText } from "../components/ui";
import { findSimilar, fullName, type SimilarPatient } from "../lib/patients";
import type { RegisterForm } from "../lib/protocol";
import { estimateDob, registerPatient, validateRegister } from "../lib/records";
import { getDeviceId, requireKeys } from "../lib/session";
import { siteToday } from "../lib/time";

const EMPTY: RegisterForm = {
  first_name: "",
  middle_name: "",
  last_name: "",
  sex: "",
  dob_mode: "exact",
  dob: "",
  age_years: "",
  age_months: "",
  mobile: "",
  residence: "",
  allergies: "",
  is_walk_in: false,
  identifiers: [],
};

export default function RegisterScreen() {
  const navigate = useNavigate();
  const { reference, profile, refresh, sync } = useApp();
  const [form, setForm] = useState<RegisterForm>(() => ({
    ...EMPTY,
    identifiers: reference?.identifier_types.filter((t) => t.is_primary).slice(0, 1).map((t) => ({ identifier_type: t.name, identifier_value: "" })) ?? [],
  }));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [similar, setSimilar] = useState<SimilarPatient[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!reference || !profile) return null;

  const set = <K extends keyof RegisterForm>(k: K, v: RegisterForm[K]) => {
    setForm((f) => ({ ...f, [k]: v }));
    setSimilar(null);
  };

  async function submit(e: FormEvent, confirmed = false) {
    e.preventDefault();
    if (!reference || !profile) return;
    const errs = validateRegister(form, reference);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    setBusy(true);
    setError(null);
    try {
      const keys = requireKeys();
      if (!confirmed) {
        const dob =
          form.dob_mode === "exact"
            ? form.dob || null
            : estimateDob(siteToday(), Number(form.age_years || 0), Number(form.age_months || 0));
        const found = await findSimilar(keys, {
          first_name: form.first_name,
          last_name: form.last_name ?? null,
          sex: form.sex,
          dob,
          dob_estimated: form.dob_mode === "estimated",
          residence: form.residence || null,
          identifiers: form.identifiers,
        });
        if (found.length) {
          setSimilar(found);
          return;
        }
      }
      const patient = await registerPatient(keys, { session: profile.session, deviceId: await getDeviceId() }, form);
      await refresh();
      void sync("record");
      navigate(`/patients/${encodeURIComponent(patient.key)}`, { replace: true });
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  const sameId = similar?.some((s) => s.sameIdentifier);
  return (
    <Page title="Register patient" back="/patients">
      <form onSubmit={(e) => submit(e)} className="space-y-4" noValidate>
        <div className="card grid gap-4 sm:grid-cols-3">
          <Field label="First name" error={errors.first_name} htmlFor="first_name">
            <TextInput id="first_name" value={form.first_name} onChange={(e) => set("first_name", e.target.value)} autoCapitalize="words" />
          </Field>
          <Field label="Middle name" htmlFor="middle_name">
            <TextInput id="middle_name" value={form.middle_name ?? ""} onChange={(e) => set("middle_name", e.target.value)} autoCapitalize="words" />
          </Field>
          <Field label="Last name" error={errors.last_name} htmlFor="last_name">
            <TextInput id="last_name" value={form.last_name ?? ""} onChange={(e) => set("last_name", e.target.value)} autoCapitalize="words" />
          </Field>
        </div>

        <div className="card space-y-4">
          <Field label="Sex" error={errors.sex}>
            <Choice name="Sex" options={reference.genders.length ? reference.genders : ["Female", "Male"]} value={form.sex} onChange={(v) => set("sex", v)} />
          </Field>
          <Field label="Date of birth">
            <Choice name="Date of birth" options={["Exact date", "Estimated age"]} value={form.dob_mode === "exact" ? "Exact date" : "Estimated age"} onChange={(v) => set("dob_mode", v === "Exact date" ? "exact" : "estimated")} />
          </Field>
          {form.dob_mode === "exact" ? (
            <Field label="Exact date of birth" error={errors.dob} htmlFor="dob">
              <TextInput id="dob" type="date" max={siteToday()} value={form.dob ?? ""} onChange={(e) => set("dob", e.target.value)} />
            </Field>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <Field label="Age (years)" error={errors.age_years} htmlFor="age_years">
                <TextInput id="age_years" inputMode="numeric" value={String(form.age_years ?? "")} onChange={(e) => set("age_years", e.target.value.replace(/\D/g, ""))} />
              </Field>
              <Field label="and months" error={errors.age_months} htmlFor="age_months">
                <TextInput id="age_months" inputMode="numeric" value={String(form.age_months ?? "")} onChange={(e) => set("age_months", e.target.value.replace(/\D/g, ""))} />
              </Field>
            </div>
          )}
        </div>

        <div className="card space-y-3">
          <h2 className="font-semibold">Identifiers</h2>
          <IdentifiersEditor types={reference.identifier_types} value={form.identifiers} onChange={(v) => set("identifiers", v)} errors={errors} />
          <label className="flex min-h-12 items-center gap-3">
            <input type="checkbox" className="h-6 w-6" checked={form.is_walk_in} onChange={(e) => set("is_walk_in", e.target.checked)} />
            Walk-in (no primary identifier available)
          </label>
          {errors.identifiers ? <p className="field-error">{errors.identifiers}</p> : null}
        </div>

        <div className="card space-y-3">
          <Field label="Residence">
            <ResidencePicker residences={reference.residences} value={form.residence ?? ""} onChange={(v) => set("residence", v)} />
          </Field>
          <Field label="Mobile" error={errors.mobile} htmlFor="mobile">
            <TextInput id="mobile" type="tel" inputMode="tel" value={form.mobile ?? ""} onChange={(e) => set("mobile", e.target.value)} />
          </Field>
          <Field label="Allergies" htmlFor="allergies">
            <TextArea id="allergies" rows={2} value={form.allergies ?? ""} onChange={(e) => set("allergies", e.target.value)} />
          </Field>
        </div>

        {similar ? (
          <div className="space-y-2">
            <Alert kind={sameId ? "error" : "warn"}>
              {sameId
                ? "A patient on this device already has this identifier. Open that patient instead of registering again."
                : "Possible duplicates on this device. Check them before registering a new patient."}
            </Alert>
            {similar.map((s) => (
              <Link key={s.patient.key} to={`/patients/${encodeURIComponent(s.patient.key)}`} className="card block">
                <div className="font-semibold">{fullName(s.patient)}</div>
                <div className="text-sm text-slate-600">
                  {s.patient.sex} · born {s.patient.dob} · {s.patient.name ?? "registered on this device"}
                </div>
                <div className="text-xs text-slate-600">{s.reasons.join(", ")}</div>
              </Link>
            ))}
            <button type="button" className="btn-danger w-full" disabled={busy} onClick={(e) => submit(e as unknown as FormEvent, true)}>
              Not the same person: register anyway
            </button>
            <p className="text-xs text-slate-500">The server checks again at sync and raises a conflict for the HIS Officer if it finds a duplicate.</p>
          </div>
        ) : null}

        {error ? <Alert kind="error">{error}</Alert> : null}
        {Object.keys(errors).length ? <Alert kind="error">Check the highlighted fields.</Alert> : null}
        <button type="submit" className="btn-primary w-full" disabled={busy}>
          {busy ? "Checking…" : "Register"}
        </button>
      </form>
    </Page>
  );
}
