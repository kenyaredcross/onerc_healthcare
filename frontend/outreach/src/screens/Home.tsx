import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { RenewPanel } from "../components/Renew";
import { SessionEndedPanel } from "../components/SessionEnded";
import { Alert, Page, Stat, errorText, formatWhen } from "../components/ui";
import { SignInForm } from "./SignIn";
import { getKeys, markSessionReturned } from "../lib/session";
import { deleteDraft, listDrafts, type VisitDraft } from "../lib/visit";

export default function HomeScreen() {
  const navigate = useNavigate();
  const app = useApp();
  const { profile, counts, syncState, authRequired, online, drafts, lockNow, refresh } = app;
  const [draftList, setDraftList] = useState<VisitDraft[]>([]);

  useEffect(() => {
    const keys = getKeys();
    if (keys) void listDrafts(keys).then(setDraftList);
  }, [drafts]);

  return (
    <Page
      title={profile?.session_title || "KRCS Outreach"}
      actions={
        <button type="button" className="btn-secondary min-h-11 px-3 text-sm" onClick={lockNow}>
          Lock
        </button>
      }
    >
      <div className="text-sm text-slate-600">
        {profile?.full_name} · {profile?.facility_name} · session {profile?.session}
        <br />
        Offline use until {profile?.expires_at}
      </div>
      <SessionEndedPanel />
      {authRequired ? <RenewPanel reason="The server did not accept this device. Sign in online to keep syncing; nothing has been lost." onDone={() => void app.sync("manual")} /> : null}
      {!online ? <Alert kind="warn">Offline: everything you record is kept on this device and synced when you are back online.</Alert> : null}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Patients on device" value={app.patients} to="/patients" />
        <Stat label="Waiting to sync" value={counts?.unsynced ?? 0} to="/sync" />
        <Stat label="Need attention" value={(counts?.failed ?? 0) + (counts?.held ?? 0) + (counts?.conflict ?? 0)} to="/sync" />
        <Stat label="Synced (kept)" value={(counts?.accepted ?? 0) + (counts?.duplicate ?? 0) + (counts?.resolved ?? 0)} to="/sync" />
      </div>
      <div className="text-sm text-slate-600">
        Last successful sync: {formatWhen(syncState?.last_success_at)}
        {syncState?.last_error ? <span className="text-red-700"> · {syncState.last_error}</span> : null}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <Link to="/patients" className="btn-primary h-16 text-lg">
          Find patient / start visit
        </Link>
        <Link to="/patients/new" className="btn-secondary h-16 text-lg">
          Register new patient
        </Link>
        <Link to="/activity" className="btn-secondary h-16 text-lg">
          Health talk / home visit
        </Link>
        <Link to="/sync" className="btn-secondary h-16 text-lg">
          Sync
        </Link>
      </div>

      {draftList.length ? (
        <section className="space-y-2">
          <h2 className="font-semibold">Unfinished visits</h2>
          {draftList.map((d) => (
            <div key={d.id} className="card flex items-center gap-3">
              <button type="button" className="min-w-0 flex-1 text-left" onClick={() => navigate(`/visit/${d.id}`)}>
                <div className="truncate font-medium">
                  {d.correcting ? "Correction: " : ""}
                  {d.patient.display}
                </div>
                <div className="text-sm text-slate-600">
                  {d.department} · {d.correcting ? "captured" : "started"} {d.started_at}
                </div>
              </button>
              <button
                type="button"
                className="btn-danger min-h-11 px-3 text-sm"
                onClick={async () => {
                  const what = d.correcting
                    ? `Discard this correction of ${d.patient.display}'s visit? The failed record stays on the device as it was.`
                    : `Discard the unfinished visit of ${d.patient.display}?`;
                  if (!window.confirm(what)) return;
                  await deleteDraft(d.id);
                  await refresh();
                }}
              >
                Discard
              </button>
            </div>
          ))}
        </section>
      ) : null}

      <div className="flex flex-wrap gap-3 pt-2">
        <Link to="/download" className="btn-secondary">
          Update package
        </Link>
      </div>
      <TeamLead />
      <About />
    </Page>
  );
}

/** About the app, with the device-security note (review M5, accepted risk). */
export function About() {
  return (
    <details className="card">
      <summary className="cursor-pointer text-sm font-semibold">About this app and device security</summary>
      <div className="mt-3 space-y-2 text-sm text-slate-700">
        <p>
          KRCS Outreach keeps the session's package and every record you capture encrypted on this device until the facility server has saved them.
        </p>
        <p className="font-medium">
          The PIN only protects this app. The tablet's own encryption and screen lock must be switched on (Settings, Security), and the tablet must
          never be shared unlocked. A lost or stolen tablet must be reported to the HIS Officer at once so the device can be revoked.
        </p>
      </div>
    </details>
  );
}

/** Team lead: mark the session returned (online sign-in; the server checks the lead flag). */
function TeamLead() {
  const { signedInOnline, online, counts, refresh } = useApp();
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <details className="card">
      <summary className="cursor-pointer text-sm font-semibold">Team lead: end of the outreach</summary>
      <div className="mt-3 space-y-3">
        <p className="text-sm text-slate-700">When the team is back, the team lead marks the session as returned. Sync every device first.</p>
        {counts && counts.unsynced > 0 ? <Alert kind="warn">{counts.unsynced} record(s) on this device are not synced yet.</Alert> : null}
        {!signedInOnline ? (
          <SignInForm onDone={() => void refresh()} hint="Marking the session returned needs an online sign-in." />
        ) : (
          <button
            type="button"
            className="btn-secondary w-full"
            disabled={busy || !online}
            onClick={async () => {
              if (!window.confirm("Mark this outreach session as returned?")) return;
              setBusy(true);
              setMessage(null);
              try {
                const res = await markSessionReturned();
                setMessage({ kind: "ok", text: `Session ${res.name} is now ${res.status}.` });
              } catch (e) {
                setMessage({ kind: "error", text: errorText(e) });
              } finally {
                setBusy(false);
              }
            }}
          >
            Mark session returned
          </button>
        )}
        {message ? <Alert kind={message.kind}>{message.text}</Alert> : null}
      </div>
    </details>
  );
}
