import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { Alert, Page, errorText } from "../components/ui";
import { ApiError } from "../lib/api";
import { downloadPackage, type DownloadProgress } from "../lib/package";
import { deviceCredentials, getProfile, markAuthRequired, requireKeys } from "../lib/session";

export default function DownloadScreen() {
  const navigate = useNavigate();
  const { refresh, hasPackage, online } = useApp();
  const [progress, setProgress] = useState<DownloadProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const started = useRef(false);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const profile = await getProfile();
      if (!profile) throw new Error("This device is not enrolled.");
      await downloadPackage({
        keys: requireKeys(),
        session: profile.session,
        credentials: await deviceCredentials(),
        onProgress: setProgress,
      });
      await refresh();
      navigate("/", { replace: true });
    } catch (e) {
      if (e instanceof ApiError && e.needsSignIn) await markAuthRequired();
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!hasPackage && !started.current && online) {
      started.current = true;
      void start();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pct = progress && progress.total ? Math.round((progress.loaded / progress.total) * 100) : 0;
  return (
    <Page title="Download package" back={hasPackage ? "/" : undefined}>
      <p className="text-slate-700">
        The package holds the reference lists (diagnoses, medicines, lab tests, places) and the patients of the session's catchment area. It is
        stored encrypted on this device.
      </p>
      {!online ? <Alert kind="warn">You are offline. Connect to download the package.</Alert> : null}
      {progress ? (
        <div className="card space-y-2">
          <div className="text-sm font-medium">
            {progress.phase === "reference" && "Reference lists…"}
            {progress.phase === "patients" && `Patients: ${progress.loaded} of ${progress.total}`}
            {progress.phase === "done" && `Done: ${progress.loaded} patients`}
          </div>
          <div className="h-3 w-full overflow-hidden rounded bg-slate-200" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
            <div className="h-full bg-krcs transition-all" style={{ width: `${progress.phase === "done" ? 100 : pct}%` }} />
          </div>
        </div>
      ) : null}
      {error ? <Alert kind="error">{error}</Alert> : null}
      <button type="button" className="btn-primary w-full" disabled={busy || !online} onClick={start}>
        {busy ? "Downloading…" : hasPackage ? "Update package" : "Download package"}
      </button>
    </Page>
  );
}
