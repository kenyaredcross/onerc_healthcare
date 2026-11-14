import { useState, type FormEvent, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { Alert, Choice, Field, Page, TextArea, TextInput, errorText } from "../components/ui";
import type { CommunityActivityForm } from "../lib/protocol";
import { recordActivity, validateActivity } from "../lib/records";
import { getDeviceId, requireKeys } from "../lib/session";
import { siteToday } from "../lib/time";

export default function ActivityScreen() {
  const { reference, profile } = useApp();
  if (!reference || !profile) return null;
  return (
    <ActivityEditor
      title="Community activity"
      back="/"
      done="/"
      submitLabel="Save activity"
      initial={{
        activity_type: reference.community_activity_types[0] ?? "",
        activity_date: siteToday(),
        location: "",
        topic: "",
        participants_male: "",
        participants_female: "",
        notes: "",
      }}
      onSave={async (form) => {
        await recordActivity(requireKeys(), { session: profile.session, deviceId: await getDeviceId() }, form);
      }}
    />
  );
}

/** The community activity form: a new record, or the correction of a refused one (F2). */
export function ActivityEditor({
  title,
  back,
  done,
  submitLabel,
  initial,
  onSave,
  intro,
}: {
  title: string;
  back: string;
  done: string;
  submitLabel: string;
  initial: CommunityActivityForm;
  onSave: (form: CommunityActivityForm) => Promise<void>;
  intro?: ReactNode;
}) {
  const navigate = useNavigate();
  const { reference, profile, refresh, sync } = useApp();
  const [form, setForm] = useState<CommunityActivityForm>(initial);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!reference || !profile) return null;
  const set = <K extends keyof CommunityActivityForm>(k: K, v: CommunityActivityForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const total = Number(form.participants_male || 0) + Number(form.participants_female || 0);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!reference || !profile) return;
    const errs = validateActivity(form, reference);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    setBusy(true);
    setError(null);
    try {
      await onSave(form);
      await refresh();
      void sync("record");
      navigate(done, { replace: true });
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Page title={title} back={back}>
      {intro}
      <form onSubmit={submit} className="card space-y-4" noValidate>
        <Field label="Activity" error={errors.activity_type}>
          <Choice name="Activity" options={reference.community_activity_types} value={form.activity_type} onChange={(v) => set("activity_type", v)} />
        </Field>
        <Field label="Date" error={errors.activity_date}>
          <TextInput type="date" max={siteToday()} value={form.activity_date} onChange={(e) => set("activity_date", e.target.value)} />
        </Field>
        <Field label="Location">
          <TextInput value={form.location ?? ""} onChange={(e) => set("location", e.target.value)} placeholder="e.g. Village 3 market" />
        </Field>
        <Field label="Topic">
          <TextInput value={form.topic ?? ""} onChange={(e) => set("topic", e.target.value)} />
        </Field>
        <div className="grid grid-cols-3 gap-3">
          <Field label="Male" error={errors.participants_male}>
            <TextInput inputMode="numeric" value={String(form.participants_male ?? "")} onChange={(e) => set("participants_male", e.target.value.replace(/\D/g, ""))} />
          </Field>
          <Field label="Female" error={errors.participants_female}>
            <TextInput inputMode="numeric" value={String(form.participants_female ?? "")} onChange={(e) => set("participants_female", e.target.value.replace(/\D/g, ""))} />
          </Field>
          <Field label="Total">
            <div className="input bg-slate-100">{total}</div>
          </Field>
        </div>
        <Field label="Notes">
          <TextArea value={form.notes ?? ""} onChange={(e) => set("notes", e.target.value)} />
        </Field>
        {error ? <Alert kind="error">{error}</Alert> : null}
        <button type="submit" className="btn-primary w-full" disabled={busy}>
          {busy ? "Saving…" : submitLabel}
        </button>
      </form>
    </Page>
  );
}
