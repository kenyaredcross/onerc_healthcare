// @vitest-environment jsdom
/** Fix round 1 in the running app: the shell boot without token (F7), the ended session (F1), correcting a failed record (F2). */
import "fake-indexeddb/auto";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import App from "../src/App";
import { AppProvider } from "../src/app/AppContext";
import { apiConfig } from "../src/lib/api";
import { putReference, setMeta } from "../src/lib/db";
import { allRows, getRow, readSecret } from "../src/lib/outbox";
import { DEFAULT_SETTINGS, type PushArgs, type RecordResult, type ReferenceData } from "../src/lib/protocol";
import { recordActivity } from "../src/lib/records";
import { enrolNew, getDeviceId, getKeys, onlineUser, resetMemory, setPin, signInOnline } from "../src/lib/session";
import { setSessionStatus } from "../src/lib/sessionStatus";
import { formatSiteTime, siteToday } from "../src/lib/time";
import { freshDevice, mockServer, type Call } from "./helpers";

const SESSION = "KLB-OUT-2026-00001";
const PUSH = "onerc_healthcare.outreach.sync.push";
const DATA: ReferenceData = {
  icd11: [],
  medications: [],
  dosages: [],
  periods: [],
  dosage_forms: [],
  lab_templates: [],
  departments: ["Outpatient"],
  identifier_types: [],
  residences: [],
  triage_levels: ["Non-urgent"],
  community_activity_types: ["Health talk", "Home visit"],
  outcomes: ["Discharged home"],
  genders: ["Female", "Male"],
};

function renderApp(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <AppProvider>
        <App />
      </AppProvider>
    </MemoryRouter>,
  );
}

const fullBoot = () => ({
  site_name: "emr-test.localhost",
  csrf_token: "csrf-from-get-boot",
  timezone: "Africa/Nairobi",
  user: { name: "nurse@krcs.test", full_name: "Nurse Amina" },
  sw_url: "/outreach/sw.js",
  build: null,
});

function server(push: (c: Call) => unknown) {
  return mockServer({
    login: () => ({ message: "Logged In" }),
    "onerc_healthcare.www.outreach.get_boot": fullBoot,
    "onerc_healthcare.outreach.devices.my_sessions": () => [],
    "onerc_healthcare.outreach.devices.enrol": () => ({
      grant: "G",
      secret: "S",
      expires_at: formatSiteTime(Date.now() + 86_400_000),
      user: { name: "nurse@krcs.test", full_name: "Nurse Amina" },
      settings: DEFAULT_SETTINGS,
    }),
    [PUSH]: push,
  });
}

/** An unlocked device with its package, ready for the home screen. */
async function readyDevice(): Promise<void> {
  await signInOnline("nurse@krcs.test", "pw");
  await enrolNew(
    { name: SESSION, title: "Village 3", facility: "F", facility_name: "Natukobenyo", start_date: "2026-11-20", end_date: "2026-11-21", catchment_area: "C", status: "Active", departments: ["Outpatient"] },
    "Tablet",
  );
  await setPin("123456");
  await putReference(getKeys()!, "v1", DATA);
  await setMeta("package_at", Date.now());
}

describe("fix round 1 in the app (jsdom)", () => {
  beforeEach(async () => {
    await freshDevice();
    window.confirm = () => true;
    delete window.__OUTREACH_BOOT__;
  });

  it("F7: the page boot has no token or user; the app takes both from get_boot", async () => {
    window.__OUTREACH_BOOT__ = { site_name: "emr-test.localhost", timezone: "Africa/Nairobi", sw_url: "/outreach/sw.js", build: "1" };
    const { calls } = server(() => ({ batch: "B", results: [] }));
    renderApp("/");
    await waitFor(() => expect(onlineUser()?.name).toBe("nurse@krcs.test"));
    expect(apiConfig.csrfToken).toBe("csrf-from-get-boot");
    expect(calls.some((c) => c.path === "onerc_healthcare.www.outreach.get_boot")).toBe(true);
    // signed in online, not enrolled: the session picker
    expect(await screen.findByRole("heading", { name: "Choose your outreach session" })).toBeTruthy();
  });

  it("F1: a Cancelled session with unsynced records says to contact the HIS Officer and offers no reset", async () => {
    server(() => new Response()); // never reached: the push is refused below
    await readyDevice();
    await recordActivity(getKeys()!, { session: SESSION, deviceId: await getDeviceId() }, { activity_type: "Health talk", activity_date: "2026-11-20", participants_female: 4 });
    mockServer({
      "onerc_healthcare.www.outreach.get_boot": fullBoot,
      "onerc_healthcare.outreach.devices.my_sessions": () => [],
      [PUSH]: () => {
        throw "network";
      },
    });
    await setSessionStatus("Cancelled", "Outreach session is Cancelled");
    renderApp("/");
    expect(await screen.findByText(/was cancelled at the facility, and the server no longer accepts its records/)).toBeTruthy();
    expect(screen.getByText(/Contact the HIS Officer/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Start a new session" })).toBeNull();
    expect(await allRows()).toHaveLength(1);
  });

  it("F1: a Closed session with every record final offers Start a new session, which leads to the session picker", async () => {
    server((c) => ({ batch: "B", results: (c.body as PushArgs).records.map((r) => ({ client_uuid: r.client_uuid, result: "accepted", doctype: "X", name: "X-1", conflict: null, error: null })) }));
    await readyDevice();
    await setSessionStatus("Closed");
    renderApp("/");
    const button = await screen.findByRole("button", { name: "Start a new session" });
    await act(async () => {
      fireEvent.click(button);
    });
    expect(await screen.findByRole("heading", { name: "Choose your outreach session" })).toBeTruthy();
    expect(getKeys()).toBeNull();
  });

  it("F2: a failed record opens prefilled from the Sync screen and is re-sent with the same uuid", async () => {
    let fail = true;
    const answer = (uuid: string): RecordResult =>
      fail
        ? { client_uuid: uuid, result: "failed", doctype: null, name: null, conflict: null, error: "Unknown activity type." }
        : { client_uuid: uuid, result: "accepted", doctype: "Community Activity Record", name: "CAR-1", conflict: null, error: null };
    const { calls } = server((c) => ({ batch: "B", results: (c.body as PushArgs).records.map((r) => answer(r.client_uuid)) }));
    await readyDevice();
    const uuid = await recordActivity(getKeys()!, { session: SESSION, deviceId: await getDeviceId() }, { activity_type: "Health talk", activity_date: siteToday(), topic: "Hygiene", participants_female: 4 });
    renderApp("/sync");
    const fix = await screen.findByRole("button", { name: "Correct and send again" });
    expect(screen.getByText("Unknown activity type.")).toBeTruthy();
    await act(async () => {
      fireEvent.click(fix);
    });
    expect(await screen.findByRole("heading", { name: "Correct activity" })).toBeTruthy();
    expect((screen.getByDisplayValue("Hygiene") as HTMLInputElement).value).toBe("Hygiene");
    fail = false;
    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: "Home visit" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save and send again" }));
    });
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Correct activity" })).toBeNull());
    await waitFor(async () => expect((await getRow(uuid))?.status).toBe("accepted"));
    const sent = calls.filter((c) => c.path === PUSH).flatMap((c) => (c.body as PushArgs).records);
    expect(new Set(sent.map((r) => r.client_uuid))).toEqual(new Set([uuid]));
    expect((sent.at(-1)!.payload as { activity_type: string }).activity_type).toBe("Home visit");
    expect((await readSecret(getKeys()!, (await getRow(uuid))!)).history?.map((h) => h.kind)).toContain("corrected");
    resetMemory();
  });
});
