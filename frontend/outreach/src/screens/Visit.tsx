import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { Alert, Choice, Field, Page, Select, Spinner, TextArea, TextInput, errorText } from "../components/ui";
import { searchIcd } from "../lib/icd";
import type { LoadedReference } from "../lib/reference";
import { finishVisit } from "../lib/records";
import { getDeviceId, requireKeys } from "../lib/session";
import { siteNow } from "../lib/time";
import {
  STEPS,
  buildVisitCapture,
  diagnosisFromIcd,
  firstInvalidStep,
  loadDraft,
  saveDraft,
  validateStep,
  type DraftLab,
  type DraftPrescription,
  type StepErrors,
  type VisitDraft,
} from "../lib/visit";
import { VITAL_SPECS } from "../lib/vitals";

type Update = (fn: (d: VisitDraft) => VisitDraft) => void;

function VitalsStep({ draft, update, errors, rd }: { draft: VisitDraft; update: Update; errors: StepErrors; rd: LoadedReference }) {
  const v = draft.vitals;
  const setV = (k: string, value: string) => update((d) => ({ ...d, vitals: { ...d.vitals, [k]: value } }));
  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-600">Optional. If you record vitals, the triage level, chief complaint and at least one value are needed.</p>
      <Field label="Triage level" error={errors.triage_level}>
        <Choice name="Triage level" options={rd.triage_levels} value={v.triage_level ?? ""} onChange={(x) => setV("triage_level", x)} />
      </Field>
      <Field label="Chief complaint" error={errors.chief_complaint}>
        <TextInput value={v.chief_complaint ?? ""} onChange={(e) => setV("chief_complaint", e.target.value)} />
      </Field>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {VITAL_SPECS.map((s) => (
          <Field key={s.field} label={`${s.label} (${s.unit})`} error={errors[s.field]}>
            <TextInput inputMode="decimal" placeholder={s.example} value={v[s.field] ?? ""} onChange={(e) => setV(s.field, e.target.value)} />
          </Field>
        ))}
      </div>
      {errors.bp ? <Alert kind="error">{errors.bp}</Alert> : null}
      {errors._form ? <Alert kind="error">{errors._form}</Alert> : null}
    </div>
  );
}

function ConsultationStep({ draft, update, errors, rd }: { draft: VisitDraft; update: Update; errors: StepErrors; rd: LoadedReference }) {
  const c = draft.consultation;
  const [q, setQ] = useState("");
  const hits = useMemo(() => searchIcd(rd.icdIndex, q, 25), [rd.icdIndex, q]);
  const setC = (patch: Partial<VisitDraft["consultation"]>) => update((d) => ({ ...d, consultation: { ...d.consultation, ...patch } }));
  const notes = [
    ["presenting_complaint", "Presenting complaint"],
    ["history", "History"],
    ["examination_findings", "Examination findings"],
    ["management_plan", "Management plan"],
  ] as const;
  return (
    <div className="space-y-4">
      {notes.map(([k, label]) => (
        <Field key={k} label={label}>
          <TextArea rows={2} value={c[k]} onChange={(e) => setC({ [k]: e.target.value })} />
        </Field>
      ))}
      <section className="space-y-2">
        <h3 className="font-semibold">Diagnoses (ICD-11)</h3>
        {c.diagnoses.map((d, i) => (
          <div key={d.code} className="rounded-lg border border-slate-200 p-2">
            <div className="flex items-start justify-between gap-2">
              <div>
                <span className="font-mono font-semibold">{d.code_value}</span> {d.title}
              </div>
              <button type="button" className="btn-secondary min-h-10 px-3" aria-label={`Remove ${d.code_value}`} onClick={() => setC({ diagnoses: c.diagnoses.filter((_, j) => j !== i) })}>
                ×
              </button>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-3">
              <Choice
                name="Case type"
                options={["New", "Follow-up"]}
                value={d.case_type}
                onChange={(v) => setC({ diagnoses: c.diagnoses.map((x, j) => (j === i ? { ...x, case_type: v as "New" | "Follow-up" } : x)) })}
              />
              <label className="flex min-h-12 items-center gap-2">
                <input
                  type="radio"
                  name="primary"
                  className="h-5 w-5"
                  checked={d.is_primary}
                  onChange={() => setC({ diagnoses: c.diagnoses.map((x, j) => ({ ...x, is_primary: j === i })) })}
                />
                Primary
              </label>
            </div>
          </div>
        ))}
        {errors.diagnoses ? <p className="field-error">{errors.diagnoses}</p> : null}
        <TextInput placeholder="Search code or title, e.g. 1F40 or malaria" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search ICD-11" />
        {q.trim() ? (
          <ul className="max-h-72 divide-y overflow-auto rounded-lg border border-slate-200 bg-white">
            {hits.map((h) => (
              <li key={h.name}>
                <button
                  type="button"
                  className="flex min-h-12 w-full items-start gap-2 px-2 py-1 text-left disabled:opacity-40"
                  disabled={c.diagnoses.some((d) => d.code === h.name)}
                  onClick={() => {
                    setC({ diagnoses: [...c.diagnoses, diagnosisFromIcd(h, c.diagnoses.length === 0)], no_diagnosis_reason: "" });
                    setQ("");
                  }}
                >
                  <span className="w-20 shrink-0 font-mono font-semibold">{h.code}</span>
                  <span className={h.leaf ? "" : "text-slate-500"}>{h.title}</span>
                </button>
              </li>
            ))}
            {hits.length === 0 ? <li className="p-2 text-sm text-slate-600">No match.</li> : null}
          </ul>
        ) : null}
      </section>
      {c.diagnoses.length === 0 ? (
        <Field label="Reason there is no diagnosis" error={errors.no_diagnosis_reason}>
          <TextInput value={c.no_diagnosis_reason} onChange={(e) => setC({ no_diagnosis_reason: e.target.value })} />
        </Field>
      ) : null}
    </div>
  );
}

function LabsStep({ draft, update, errors, rd }: { draft: VisitDraft; update: Update; errors: StepErrors; rd: LoadedReference }) {
  const [choice, setChoice] = useState("");
  const setLab = (id: string, patch: Partial<DraftLab>) => update((d) => ({ ...d, labs: d.labs.map((l) => (l.line_id === id ? { ...l, ...patch } : l)) }));
  const available = rd.lab_templates.filter((t) => !draft.labs.some((l) => l.template === t.name));
  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-600">Rapid (point-of-care) tests can be resulted here. Other tests are sent to the facility laboratory.</p>
      {draft.labs.map((l) => {
        const t = rd.lab_templates.find((x) => x.name === l.template);
        return (
          <div key={l.line_id} className="card space-y-2">
            <div className="flex items-start justify-between gap-2">
              <div className="font-semibold">
                {l.template} {t?.point_of_care ? <span className="ml-1 rounded bg-green-100 px-1 text-xs text-green-800">POC</span> : null}
              </div>
              <button type="button" className="btn-secondary min-h-10 px-3" aria-label={`Remove ${l.template}`} onClick={() => update((d) => ({ ...d, labs: d.labs.filter((x) => x.line_id !== l.line_id) }))}>
                ×
              </button>
            </div>
            {t?.point_of_care ? (
              <Field label={`Result${t.unit ? ` (${t.unit})` : ""} — leave empty to send to the lab`}>
                {t.options?.length ? (
                  <Select
                    options={t.options}
                    placeholder="Not resulted"
                    value={l.result}
                    onChange={(e) => setLab(l.line_id, { result: e.target.value, resulted_at: e.target.value ? siteNow() : null })}
                  />
                ) : (
                  <TextInput
                    inputMode={t.data_type === "Float" || t.data_type === "Int" ? "decimal" : "text"}
                    value={l.result}
                    onChange={(e) => setLab(l.line_id, { result: e.target.value, resulted_at: e.target.value ? siteNow() : null })}
                  />
                )}
              </Field>
            ) : (
              <p className="text-sm text-slate-600">Sample to the facility laboratory.</p>
            )}
            {errors[l.line_id] ? <p className="field-error">{errors[l.line_id]}</p> : null}
          </div>
        );
      })}
      <div className="flex gap-2">
        <Select
          className="flex-1"
          aria-label="Lab test"
          options={available.map((t) => ({ value: t.name, label: `${t.name}${t.point_of_care ? " (POC)" : ""}` }))}
          placeholder="Choose a test…"
          value={choice}
          onChange={(e) => setChoice(e.target.value)}
        />
        <button
          type="button"
          className="btn-secondary"
          disabled={!choice}
          onClick={() => {
            const t = rd.lab_templates.find((x) => x.name === choice);
            if (!t) return;
            update((d) => ({ ...d, labs: [...d.labs, { line_id: `l${Date.now().toString(36)}`, template: t.name, poc: Boolean(t.point_of_care), result: "", resulted_at: null }] }));
            setChoice("");
          }}
        >
          Add
        </button>
      </div>
    </div>
  );
}

function PrescriptionsStep({ draft, update, errors, rd }: { draft: VisitDraft; update: Update; errors: StepErrors; rd: LoadedReference }) {
  const [q, setQ] = useState("");
  const query = q.trim().toLowerCase();
  const hits = query
    ? rd.medications
        .filter((m) => `${m.name} ${m.generic_name ?? ""}`.toLowerCase().includes(query))
        .slice(0, 20)
    : [];
  const setRx = (id: string, patch: Partial<DraftPrescription>) =>
    update((d) => ({ ...d, prescriptions: d.prescriptions.map((p) => (p.line_id === id ? { ...p, ...patch } : p)) }));
  return (
    <div className="space-y-4">
      {draft.prescriptions.map((p) => (
        <div key={p.line_id} className="card space-y-3">
          <div className="flex items-start justify-between gap-2">
            <div className="font-semibold">{p.label}</div>
            <button type="button" className="btn-secondary min-h-10 px-3" aria-label={`Remove ${p.label}`} onClick={() => update((d) => ({ ...d, prescriptions: d.prescriptions.filter((x) => x.line_id !== p.line_id) }))}>
              ×
            </button>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label="Dosage">
              <Select options={rd.dosages} placeholder="—" value={p.dosage} onChange={(e) => setRx(p.line_id, { dosage: e.target.value })} />
            </Field>
            <Field label="Period">
              <Select options={rd.periods} placeholder="—" value={p.period} onChange={(e) => setRx(p.line_id, { period: e.target.value })} />
            </Field>
            <Field label="Dosage form">
              <Select options={rd.dosage_forms} placeholder="—" value={p.dosage_form} onChange={(e) => setRx(p.line_id, { dosage_form: e.target.value })} />
            </Field>
          </div>
          <Field label="Comment">
            <TextInput value={p.comment} onChange={(e) => setRx(p.line_id, { comment: e.target.value })} />
          </Field>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex min-h-12 items-center gap-2">
              <input
                type="checkbox"
                className="h-6 w-6"
                checked={p.dispensed}
                onChange={(e) => setRx(p.line_id, { dispensed: e.target.checked, dispensed_at: e.target.checked ? siteNow() : null })}
              />
              Dispensed now
            </label>
            {p.dispensed ? (
              <Field label="Quantity">
                <TextInput className="w-32" inputMode="decimal" value={p.quantity} onChange={(e) => setRx(p.line_id, { quantity: e.target.value })} />
              </Field>
            ) : (
              <span className="text-sm text-slate-600">Otherwise it goes to the facility pharmacy.</span>
            )}
          </div>
          {errors[p.line_id] ? <p className="field-error">{errors[p.line_id]}</p> : null}
        </div>
      ))}
      <TextInput placeholder="Search medication" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search medication" />
      {hits.length ? (
        <ul className="max-h-72 divide-y overflow-auto rounded-lg border border-slate-200 bg-white">
          {hits.map((m) => (
            <li key={m.name}>
              <button
                type="button"
                className="min-h-12 w-full px-2 py-1 text-left"
                onClick={() => {
                  update((d) => ({
                    ...d,
                    prescriptions: [
                      ...d.prescriptions,
                      {
                        line_id: `p${Date.now().toString(36)}`,
                        medication: m.name,
                        label: [m.name, m.strength].filter(Boolean).join(" "),
                        dosage: m.default_dosage ?? "",
                        period: m.default_period ?? "",
                        dosage_form: m.dosage_form ?? "",
                        comment: "",
                        dispensed: false,
                        quantity: "",
                        dispensed_at: null,
                      },
                    ],
                  }));
                  setQ("");
                }}
              >
                <div className="font-medium">{m.name}</div>
                <div className="text-xs text-slate-600">{[m.generic_name, m.strength, m.dosage_form].filter(Boolean).join(" · ")}</div>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function ReviewStep({ draft }: { draft: VisitDraft }) {
  const p = buildVisitCapture(draft);
  return (
    <div className="space-y-3 text-sm">
      <div className="card">
        <div className="font-semibold">{draft.patient.display}</div>
        <div>
          {draft.department} · started {draft.started_at}
        </div>
      </div>
      <div className="card">
        <div className="font-semibold">Vitals</div>
        {p.vitals ? (
          <div>
            {p.vitals.triage_level} · {p.vitals.chief_complaint}
            <br />
            {VITAL_SPECS.filter((s) => p.vitals?.[s.field] !== null).map((s) => `${s.label} ${p.vitals?.[s.field]} ${s.unit}`).join(" · ")}
          </div>
        ) : (
          <div className="text-slate-600">Not taken</div>
        )}
      </div>
      <div className="card">
        <div className="font-semibold">Diagnoses</div>
        {draft.consultation.diagnoses.map((d) => (
          <div key={d.code}>
            {d.code_value} {d.title} ({d.case_type}
            {d.is_primary ? ", primary" : ""})
          </div>
        ))}
        {!draft.consultation.diagnoses.length ? <div>No diagnosis: {draft.consultation.no_diagnosis_reason}</div> : null}
      </div>
      <div className="card">
        <div className="font-semibold">Labs</div>
        {draft.labs.length ? draft.labs.map((l) => <div key={l.line_id}>{l.template}: {l.result || "to facility lab"}</div>) : <div className="text-slate-600">None</div>}
      </div>
      <div className="card">
        <div className="font-semibold">Prescriptions</div>
        {draft.prescriptions.length ? (
          draft.prescriptions.map((x) => (
            <div key={x.line_id}>
              {x.label} {x.dosage} {x.period} {x.dispensed ? `· dispensed ${x.quantity}` : "· to pharmacy"}
            </div>
          ))
        ) : (
          <div className="text-slate-600">None</div>
        )}
      </div>
      <div className="card">
        <span className="font-semibold">Outcome: </span>
        {draft.outcome}
      </div>
    </div>
  );
}

export default function VisitScreen() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const { reference, profile, refresh, sync } = useApp();
  const [draft, setDraft] = useState<VisitDraft | null | undefined>(undefined);
  const [errors, setErrors] = useState<StepErrors>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    void loadDraft(requireKeys(), id).then(setDraft);
  }, [id]);

  // every change is saved (encrypted) shortly after, so a lock or a closed tab loses nothing
  useEffect(() => {
    if (!draft) return;
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => void saveDraft(requireKeys(), draft).catch(() => undefined), 300);
    return () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
    };
  }, [draft]);

  if (draft === undefined) return <Spinner />;
  if (!draft || !reference || !profile) {
    return (
      <Page title="Visit" back="/">
        <Alert kind="error">This visit is no longer on the device.</Alert>
      </Page>
    );
  }
  const d = draft;
  const rd = reference;
  const update: Update = (fn) => setDraft((cur) => (cur ? fn(cur) : cur));
  const goTo = (step: number) => {
    setErrors({});
    update((x) => ({ ...x, step }));
    window.scrollTo?.(0, 0);
  };
  const next = () => {
    const errs = validateStep(d, d.step, rd);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    goTo(Math.min(d.step + 1, STEPS.length - 1));
  };

  async function finish() {
    const bad = firstInvalidStep(d, rd);
    if (bad >= 0) {
      goTo(bad);
      setErrors(validateStep(d, bad, rd));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      await finishVisit(requireKeys(), { session: profile!.session, deviceId: await getDeviceId() }, d, rd);
      await refresh();
      void sync("record");
      navigate(`/patients/${encodeURIComponent(d.patient.key)}`, { replace: true });
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const props = { draft: d, update, errors, rd };
  return (
    <Page title={d.patient.display} back="/">
      <nav className="-mx-1 flex gap-1 overflow-x-auto pb-1" aria-label="Visit steps">
        {STEPS.map((s, i) => (
          <button
            key={s}
            type="button"
            onClick={() => goTo(i)}
            aria-current={d.step === i ? "step" : undefined}
            className={`min-h-11 shrink-0 rounded-full px-3 text-sm font-semibold ${d.step === i ? "bg-krcs text-white" : "bg-white text-slate-700 ring-1 ring-slate-300"}`}
          >
            {i + 1}. {s}
          </button>
        ))}
      </nav>
      <section className="card">
        <h2 className="mb-3 text-lg font-bold">{STEPS[d.step]}</h2>
        {d.step === 0 ? <VitalsStep {...props} /> : null}
        {d.step === 1 ? <ConsultationStep {...props} /> : null}
        {d.step === 2 ? <LabsStep {...props} /> : null}
        {d.step === 3 ? <PrescriptionsStep {...props} /> : null}
        {d.step === 4 ? (
          <Field label="Outcome" error={errors.outcome}>
            <Choice name="Outcome" options={rd.outcomes.length ? rd.outcomes : ["Discharged home", "Died"]} value={d.outcome} onChange={(v) => update((x) => ({ ...x, outcome: v }))} />
          </Field>
        ) : null}
        {d.step === 5 ? <ReviewStep draft={d} /> : null}
      </section>
      {error ? <Alert kind="error">{error}</Alert> : null}
      <div className="fixed inset-x-0 bottom-0 border-t border-slate-200 bg-white p-3">
        <div className="mx-auto flex max-w-3xl gap-3">
          <button type="button" className="btn-secondary flex-1" disabled={d.step === 0} onClick={() => goTo(d.step - 1)}>
            Back
          </button>
          {d.step < STEPS.length - 1 ? (
            <button type="button" className="btn-primary flex-1" onClick={next}>
              Next
            </button>
          ) : (
            <button type="button" className="btn-primary flex-1" disabled={busy} onClick={finish}>
              {busy ? "Saving…" : "Finish visit"}
            </button>
          )}
        </div>
      </div>
    </Page>
  );
}
