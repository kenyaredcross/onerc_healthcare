import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useApp } from "./app/AppContext";
import { Spinner } from "./components/ui";
import ActivityScreen from "./screens/Activity";
import DownloadScreen from "./screens/Download";
import EditPatientScreen from "./screens/EditPatient";
import HomeScreen from "./screens/Home";
import PatientScreen from "./screens/Patient";
import PickSessionScreen from "./screens/PickSession";
import RegisterScreen from "./screens/Register";
import SearchScreen from "./screens/Search";
import SetPinScreen from "./screens/SetPin";
import SignInScreen from "./screens/SignIn";
import SyncScreen from "./screens/Sync";
import UnlockScreen from "./screens/Unlock";
import VisitScreen from "./screens/Visit";

/** Where the device state says the user must be, or null when the requested route is allowed. */
function gate(path: string, app: ReturnType<typeof useApp>): string | null {
  switch (app.lock) {
    case "new":
      if (path === "/signin" || path === "/sessions") return null;
      return app.signedInOnline ? "/sessions" : "/signin";
    case "needs-pin":
      return path === "/pin" ? null : "/pin";
    case "locked":
    case "expired":
    case "locked-out":
      return path === "/unlock" ? null : "/unlock";
    case "unlocked":
      if (!app.hasPackage) return path === "/download" ? null : "/download";
      if (["/signin", "/pin", "/unlock"].includes(path)) return "/";
      return null;
  }
}

export default function App() {
  const app = useApp();
  const location = useLocation();
  if (!app.ready) return <Spinner label="Opening KRCS Outreach…" />;
  const target = gate(location.pathname, app);
  if (target && target !== location.pathname) return <Navigate to={target} replace />;
  return (
    <Routes>
      <Route path="/signin" element={<SignInScreen />} />
      <Route path="/sessions" element={<PickSessionScreen />} />
      <Route path="/pin" element={<SetPinScreen />} />
      <Route path="/unlock" element={<UnlockScreen />} />
      <Route path="/download" element={<DownloadScreen />} />
      <Route path="/" element={<HomeScreen />} />
      <Route path="/patients" element={<SearchScreen />} />
      <Route path="/patients/new" element={<RegisterScreen />} />
      <Route path="/patients/:key" element={<PatientScreen />} />
      <Route path="/patients/:key/edit" element={<EditPatientScreen />} />
      <Route path="/visit/:id" element={<VisitScreen />} />
      <Route path="/activity" element={<ActivityScreen />} />
      <Route path="/sync" element={<SyncScreen />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
