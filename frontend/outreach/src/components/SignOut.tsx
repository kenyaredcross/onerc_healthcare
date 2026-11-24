import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { errorText } from "./ui";
import { signOutDuringSetup } from "../lib/session";

/**
 * "Sign out" in the header of the setup screens (choose session, set PIN, download). A second tap confirms,
 * because it cancels the setup of this device. Errors (records not synced) are shown in place.
 */
export default function SignOutButton() {
  const navigate = useNavigate();
  const { refresh } = useApp();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function signOut() {
    if (!confirming) {
      setConfirming(true);
      setError(null);
      return;
    }
    setBusy(true);
    try {
      await signOutDuringSetup();
      await refresh();
      navigate("/signin", { replace: true });
    } catch (e) {
      setError(errorText(e));
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-end">
      <button
        type="button"
        className={confirming ? "btn-primary min-h-11 px-3" : "btn-secondary min-h-11 px-3"}
        disabled={busy}
        onClick={signOut}
        onBlur={() => setConfirming(false)}
      >
        {busy ? "Signing out…" : confirming ? "Tap again to sign out" : "Sign out"}
      </button>
      {confirming ? <span className="mt-1 text-xs text-slate-600">This cancels setting up this device.</span> : null}
      {error ? (
        <span role="alert" className="mt-1 max-w-xs text-right text-xs text-red-700">
          {error}
        </span>
      ) : null}
    </div>
  );
}
