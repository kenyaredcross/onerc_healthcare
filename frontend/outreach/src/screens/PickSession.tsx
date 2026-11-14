import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { Alert, Field, Page, Spinner, TextInput, errorText } from "../components/ui";
import { ApiError } from "../lib/api";
import type { OutreachSessionSummary } from "../lib/protocol";
import { defaultDeviceLabel, enrolNew, mySessions } from "../lib/session";

export default function PickSessionScreen() {
  const navigate = useNavigate();
  const { signedInOnline, refresh } = useApp();
  const [sessions, setSessions] = useState<OutreachSessionSummary[] | null>(null);
  const [label, setLabel] = useState(defaultDeviceLabel());
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    mySessions()
      .then(setSessions)
      .catch(async (e) => {
        if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
          navigate("/signin", { replace: true });
          return;
        }
        setError(errorText(e));
        setSessions([]);
      });
  }, [navigate]);

  async function choose(s: OutreachSessionSummary) {
    setBusy(s.name);
    setError(null);
    try {
      await enrolNew(s, label.trim() || defaultDeviceLabel());
      await refresh();
      navigate("/pin", { replace: true });
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Page title="Choose your outreach session">
      {signedInOnline ? <p className="text-slate-700">Signed in as {signedInOnline.full_name || signedInOnline.name}.</p> : null}
      <Field label="Name of this device" hint="Shown to the HIS Officer, e.g. 'Tablet 3 (Kalobeyei)'." htmlFor="label">
        <TextInput id="label" value={label} onChange={(e) => setLabel(e.target.value)} />
      </Field>
      {error ? <Alert kind="error">{error}</Alert> : null}
      {sessions === null ? (
        <Spinner />
      ) : sessions.length === 0 ? (
        <Alert kind="warn">You are not on the team of any planned or active outreach session. Ask the Medical In-Charge or HIS Officer.</Alert>
      ) : (
        <ul className="space-y-3">
          {sessions.map((s) => (
            <li key={s.name}>
              <button
                type="button"
                className="card w-full text-left active:bg-slate-50 disabled:opacity-50"
                disabled={busy !== null}
                onClick={() => choose(s)}
              >
                <div className="flex items-start justify-between gap-2">
                  <span className="text-lg font-semibold">{s.title || s.name}</span>
                  <span className="rounded bg-slate-100 px-2 py-0.5 text-xs">{s.status}</span>
                </div>
                <div className="text-sm text-slate-600">
                  {s.facility_name} · {s.start_date} to {s.end_date}
                </div>
                <div className="text-sm text-slate-600">{s.departments.join(", ")}</div>
                {busy === s.name ? <div className="mt-2 text-sm text-krcs">Preparing this device…</div> : null}
              </button>
            </li>
          ))}
        </ul>
      )}
    </Page>
  );
}
