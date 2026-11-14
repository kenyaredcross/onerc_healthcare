import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { startNewSession } from "../lib/session";
import { isEnded } from "../lib/sessionStatus";
import { Alert, errorText } from "./ui";

/**
 * Fix round 1 (F1): what to do once the session has ended on the server.
 * - Closed: the remaining records still sync with this device's grant (renewed by an online sign-in).
 * - Cancelled: the server refuses them; they stay on the device and the HIS Officer must be contacted.
 * - Every record final and no unfinished visit: offer "Start a new session" (no patient data is kept).
 */
export function SessionEndedPanel() {
  const navigate = useNavigate();
  const { sessionStatus, counts, drafts, profile, refresh } = useApp();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const status = sessionStatus?.status;
  if (!isEnded(status) || !counts) return null;
  const unsynced = counts.unsynced;
  const allFinal = unsynced === 0;
  const name = profile?.session_title || profile?.session || "This outreach session";

  async function start() {
    if (!window.confirm("Remove this session's package and synced records from the device and start a new session?")) return;
    setBusy(true);
    setError(null);
    try {
      await startNewSession();
      await refresh();
      navigate("/sessions", { replace: true });
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3" data-testid="session-ended">
      {status === "Cancelled" ? (
        allFinal ? (
          <Alert kind="warn">{name} was cancelled at the facility. Every record of this device was saved before that.</Alert>
        ) : (
          <Alert kind="error">
            {name} was cancelled at the facility, and the server no longer accepts its records. {unsynced} record(s) are kept on this device and
            will not be deleted. Contact the HIS Officer before doing anything else with this device.
          </Alert>
        )
      ) : status === "Closed" ? (
        allFinal ? (
          <Alert kind="ok">{name} was closed at the facility, and every record of this device is saved there.</Alert>
        ) : (
          <Alert kind="warn">
            {name} was closed at the facility. The {unsynced} record(s) still on this device are accepted: sync them now. If the device asks for an
            online sign-in, sign in as {profile?.full_name || profile?.user}; your records are kept.
          </Alert>
        )
      ) : allFinal ? (
        <Alert kind="warn">{name} is no longer open on the server. Every record of this device is saved there.</Alert>
      ) : (
        <Alert kind="warn">
          {name} is no longer open on the server. {unsynced} record(s) are kept on this device. If the session was closed they still sync; if it was
          cancelled, contact the HIS Officer. Nothing is deleted.
        </Alert>
      )}
      {allFinal ? (
        drafts > 0 ? (
          <Alert kind="warn">
            {drafts} unfinished visit(s) are on this device. Finish them{status === "Cancelled" ? "" : " (they still sync)"} or discard them before starting a
            new session.
          </Alert>
        ) : (
          <button type="button" className="btn-primary w-full" disabled={busy} onClick={start}>
            {busy ? "Preparing…" : "Start a new session"}
          </button>
        )
      ) : null}
      {error ? <Alert kind="error">{error}</Alert> : null}
    </div>
  );
}
