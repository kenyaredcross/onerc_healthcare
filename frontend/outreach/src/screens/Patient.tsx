import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { Alert, Field, Page, Select, Spinner } from "../components/ui";
import { fullName, getPatient, type LocalPatient } from "../lib/patients";
import { residencePath } from "../lib/reference";
import { requireKeys } from "../lib/session";
import { ageLabel, siteToday } from "../lib/time";
import { newDraft, saveDraft } from "../lib/visit";

export default function PatientScreen() {
  const { key = "" } = useParams();
  const navigate = useNavigate();
  const { reference, refresh } = useApp();
  const [patient, setPatient] = useState<LocalPatient | null | undefined>(undefined);
  const [department, setDepartment] = useState("");

  useEffect(() => {
    void getPatient(requireKeys(), key).then(setPatient);
  }, [key]);

  useEffect(() => {
    if (reference?.departments.length === 1) setDepartment(reference.departments[0]!);
  }, [reference]);

  if (patient === undefined) return <Spinner />;
  if (patient === null || !reference) {
    return (
      <Page title="Patient" back="/patients">
        <Alert kind="error">This patient is not on the device.</Alert>
      </Page>
    );
  }

  async function startVisit() {
    if (!patient) return;
    const draft = newDraft(
      { key: patient.key, name: patient.name, client_uuid: patient.name ? null : patient.client_uuid, display: fullName(patient) },
      department,
    );
    await saveDraft(requireKeys(), draft);
    await refresh();
    navigate(`/visit/${draft.id}`);
  }

  const today = siteToday();
  return (
    <Page title={fullName(patient)} back="/patients">
      <div className="card space-y-1">
        <div className="text-sm text-slate-600">{patient.name ?? "Registered on this device, waiting for sync"}</div>
        <div>
          {patient.sex} · {ageLabel(patient.dob, Boolean(patient.dob_estimated), today)} · born {patient.dob}
          {patient.dob_estimated ? " (estimated)" : ""}
        </div>
        {patient.residence ? <div>Residence: {residencePath(reference, patient.residence)}</div> : null}
        {patient.mobile ? <div>Mobile: {patient.mobile}</div> : null}
        {patient.identifiers.map((i) => (
          <div key={`${i.identifier_type}${i.identifier_value}`} className="text-sm">
            {i.identifier_type}: <span className="font-mono">{i.identifier_value}</span>
          </div>
        ))}
        {patient.pending_updates ? <div className="text-sm text-amber-800">Changes waiting for sync.</div> : null}
      </div>

      <div className={`rounded-lg border p-3 ${patient.allergies ? "border-red-300 bg-red-50 text-red-900" : "border-slate-200 bg-white"}`}>
        <span className="font-semibold">Allergies: </span>
        {patient.allergies || "none recorded"}
      </div>

      <section className="card space-y-3">
        <h2 className="font-semibold">Start a visit</h2>
        <Field label="Department">
          <Select options={reference.departments} placeholder="Choose…" value={department} onChange={(e) => setDepartment(e.target.value)} />
        </Field>
        <button type="button" className="btn-primary w-full" disabled={!department} onClick={startVisit}>
          Start visit
        </button>
      </section>

      {patient.outreach_visits.length ? (
        <section className="space-y-2">
          <h2 className="font-semibold">Visits on this outreach</h2>
          {patient.outreach_visits.map((v) => (
            <div key={v.client_uuid} className="card text-sm">
              <div className="font-medium">
                {v.date} · {v.department}
              </div>
              {v.diagnoses.map((d) => (
                <div key={d.code}>
                  {d.code} {d.title} ({d.case_type})
                </div>
              ))}
              {v.prescriptions.length ? <div className="text-slate-600">Rx: {v.prescriptions.join(", ")}</div> : null}
            </div>
          ))}
        </section>
      ) : null}

      <section className="space-y-2">
        <h2 className="font-semibold">Recent visits</h2>
        {patient.recent_visits.length === 0 ? <p className="text-sm text-slate-600">No earlier visits in the package.</p> : null}
        {patient.recent_visits.map((v, i) => (
          <div key={`${v.date}${i}`} className="card text-sm">
            <div className="font-medium">
              {v.date} · {v.department} · {v.facility}
            </div>
            {v.diagnoses.map((d) => (
              <div key={d.code}>
                {d.code} {d.title} ({d.case_type})
              </div>
            ))}
          </div>
        ))}
      </section>

      <section className="space-y-2">
        <h2 className="font-semibold">Active prescriptions</h2>
        {patient.active_prescriptions.length === 0 ? <p className="text-sm text-slate-600">None.</p> : null}
        {patient.active_prescriptions.map((p, i) => (
          <div key={`${p.medication}${i}`} className="card text-sm">
            {p.medication} · {p.dosage} · {p.period} · from {p.date}
          </div>
        ))}
      </section>

      {patient.name && patient.modified ? (
        <Link to={`/patients/${encodeURIComponent(patient.key)}/edit`} className="btn-secondary w-full">
          Edit demographics
        </Link>
      ) : (
        <p className="text-xs text-slate-500">Demographics of a patient registered here can be edited after the next package download.</p>
      )}
    </Page>
  );
}
