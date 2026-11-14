import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { RenewPanel } from "../components/Renew";
import { Alert, Field, Page, TextInput, errorText } from "../components/ui";
import { SignInForm } from "./SignIn";
import { ExpiredError, LockedOutError, PinError, recoverKey, resetDevice, unlock } from "../lib/session";

function ResetDevice() {
  const { counts, refresh } = useApp();
  const [error, setError] = useState<string | null>(null);
  if (!counts || counts.unsynced > 0) return null;
  return (
    <details className="card">
      <summary className="cursor-pointer text-sm font-semibold">Use this device for another user or session</summary>
      <p className="mt-2 text-sm text-slate-700">
        Nothing is waiting to be synced. Resetting removes the downloaded package and any unfinished visits from this device.
      </p>
      {error ? <Alert kind="error">{error}</Alert> : null}
      <button
        type="button"
        className="btn-danger mt-3"
        onClick={async () => {
          if (!window.confirm("Remove all outreach data from this device?")) return;
          try {
            await resetDevice();
            await refresh();
          } catch (e) {
            setError(errorText(e));
          }
        }}
      >
        Reset this device
      </button>
    </details>
  );
}

function PinUnlock() {
  const navigate = useNavigate();
  const { profile, refresh } = useApp();
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await unlock(pin);
      setPin("");
      await refresh();
      navigate("/", { replace: true });
    } catch (err) {
      setPin("");
      if (err instanceof ExpiredError || err instanceof LockedOutError) await refresh();
      else setError(err instanceof PinError ? err.message : errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="card space-y-4">
      {profile ? (
        <p className="text-slate-700">
          {profile.full_name || profile.user} · {profile.session_title || profile.session}
        </p>
      ) : null}
      <Field label="PIN" htmlFor="unlock-pin">
        <TextInput
          id="unlock-pin"
          type="password"
          inputMode="numeric"
          autoComplete="current-password"
          autoFocus
          value={pin}
          onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))}
        />
      </Field>
      {error ? <Alert kind="error">{error}</Alert> : null}
      <button type="submit" className="btn-primary w-full" disabled={busy || !pin}>
        {busy ? "Unlocking…" : "Unlock"}
      </button>
    </form>
  );
}

function Recover() {
  const navigate = useNavigate();
  const { signedInOnline, profile, refresh } = useApp();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!signedInOnline || (profile && signedInOnline.name !== profile.user)) {
    return <SignInForm onDone={() => void refresh()} hint={profile ? `Sign in as ${profile.full_name || profile.user}.` : undefined} />;
  }
  return (
    <div className="card space-y-3">
      <p>Signed in as {signedInOnline.full_name || signedInOnline.name}. Recover access to the records on this device, then set a new PIN.</p>
      {error ? <Alert kind="error">{error}</Alert> : null}
      <button
        type="button"
        className="btn-primary w-full"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            await recoverKey();
            await refresh();
            navigate("/pin", { replace: true });
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? "Recovering…" : "Recover access"}
      </button>
    </div>
  );
}

export default function UnlockScreen() {
  const navigate = useNavigate();
  const { lock, profile } = useApp();
  return (
    <Page title="Unlock">
      {lock === "locked" ? <PinUnlock /> : null}
      {lock === "expired" ? (
        <RenewPanel
          reason={`The offline period of this device ended${profile ? ` on ${profile.expires_at}` : ""}. Sign in online to continue; your records are kept.`}
          onDone={() => navigate("/", { replace: true })}
        />
      ) : null}
      {lock === "locked-out" ? (
        <>
          <Alert kind="warn">Too many wrong PINs. The PIN has been disabled on this device. Your records are safe: sign in online to recover access.</Alert>
          <Recover />
        </>
      ) : null}
      <ResetDevice />
    </Page>
  );
}
