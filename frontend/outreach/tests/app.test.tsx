// @vitest-environment jsdom
import "fake-indexeddb/auto";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import App from "../src/App";
import { AppProvider } from "../src/app/AppContext";
import { DEFAULT_SETTINGS } from "../src/lib/protocol";
import { enrolNew, lock, setPin, signInOnline } from "../src/lib/session";
import { formatSiteTime } from "../src/lib/time";
import { freshDevice, mockServer } from "./helpers";

function renderApp(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <AppProvider>
        <App />
      </AppProvider>
    </MemoryRouter>,
  );
}

const boot = (user: string | null) => () => ({
  site_name: "emr-test.localhost",
  csrf_token: user ? "csrf" : null,
  timezone: "Africa/Nairobi",
  user: user ? { name: user, full_name: "Nurse Amina" } : null,
  sw_url: "/outreach/sw.js",
  build: null,
});

describe("app shell (jsdom)", () => {
  beforeEach(freshDevice);

  it("a new device asks for the online sign-in", async () => {
    mockServer({ "onerc_healthcare.www.outreach.get_boot": boot(null) });
    renderApp("/patients");
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(screen.getByLabelText("Email or username")).toBeTruthy();
  });

  it("an enrolled, locked device asks for the PIN and unlocks to the package download", async () => {
    let signedIn = false;
    mockServer({
      "onerc_healthcare.www.outreach.get_boot": () => boot(signedIn ? "nurse@krcs.test" : null)(),
      login: () => {
        signedIn = true;
        return { message: "Logged In" };
      },
      "onerc_healthcare.outreach.devices.enrol": () => ({
        grant: "G",
        secret: "S",
        expires_at: formatSiteTime(Date.now() + 86_400_000),
        user: { name: "nurse@krcs.test", full_name: "Nurse Amina" },
        settings: DEFAULT_SETTINGS,
      }),
      "onerc_healthcare.outreach.package.get_reference": () => {
        throw "network";
      },
    });
    await signInOnline("nurse@krcs.test", "pw");
    await enrolNew(
      { name: "KLB-OUT-2026-00001", title: "Village 3", facility: "F", facility_name: "Natukobenyo", start_date: "2026-11-20", end_date: "2026-11-21", catchment_area: "C", status: "Planned", departments: ["Outpatient"] },
      "Tablet",
    );
    await setPin("123456");
    lock();
    signedIn = false;
    renderApp("/");
    const pin = (await screen.findByLabelText("PIN")) as HTMLInputElement;
    fireEvent.change(pin, { target: { value: "000000" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Unlock" }));
    });
    expect(await screen.findByText(/Wrong PIN\. 4 attempts left\./)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("PIN"), { target: { value: "123456" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Unlock" }));
    });
    await waitFor(() => expect(screen.getByRole("heading", { name: "Download package" })).toBeTruthy());
  });
});
