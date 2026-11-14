# KRCS Outreach (offline PWA)

React 18 + TypeScript + Vite app for outreach clinics (Phase 4, SDD §5.6). The wire contract with the
server is `apps/onerc_healthcare/docs/OUTREACH_PROTOCOL.md`; every payload type is in `src/lib/protocol.ts`.

## Build and serve (production)

- `bench build --app onerc_healthcare` runs the app root `yarn build`, which runs
  `yarn install --frozen-lockfile && yarn build` here.
- Output goes to `onerc_healthcare/public/outreach/` (git-ignored), served at
  `/assets/onerc_healthcare/outreach/`.
- `www/outreach.py` renders `/outreach` and `/outreach/<path>`. It injects the hashed entry from
  `.vite/manifest.json` and `window.__OUTREACH_BOOT__` (site name, timezone, service worker URL, build).
  The service worker caches this page for offline use, so it carries **no CSRF token and no user**
  (protocol §6 F7). The app calls `onerc_healthcare.www.outreach.get_boot` at start (when online) and after
  an online sign-in to get them; nothing reads a token from the page.
- The same page serves `/outreach/sw.js`: the built service worker, with `Content-Type:
  application/javascript`, `Cache-Control: no-cache` and `Service-Worker-Allowed: /outreach/`.

## Security note (review M5, accepted risk)

The PIN only protects the app: it wraps the data key that encrypts IndexedDB (protocol §5). It does not
protect the tablet. **The tablet's own storage encryption and screen lock are required** on every outreach
device, and a lost device must be reported to the HIS Officer so it can be revoked. The app says the same
under "About this app and device security" on the home screen and when the PIN is set.

## Fix round 1 (protocol §6)

- **F1 Closed sessions.** The device keeps syncing a Closed session's records with its grant. On a 401 it
  asks for an online sign-in and renews (`enrol` again, same device, user and data key). The session status
  is learnt from `my_sessions` after an online sign-in and from refusals that name it (`src/lib/sessionStatus.ts`).
  When every record is final and no visit is unfinished, "Start a new session" removes the profile, package
  and acknowledged records (`startNewSession`). A Cancelled session with records not final shows that the HIS
  Officer must be contacted; its records are never deleted.
- **F2 Failed records.** "Correct and send again" on the Sync screen opens the record's form prefilled from the
  stored payload (registration, demographic edit, activity, or the visit stepper through a correction draft
  `fix-<uuid>`), validated as a new record, and re-queues it under the **same client_uuid**
  (`requeueFailed`). Each distinct server error and each correction is kept in the record's encrypted local
  history. Records that depend on it wait and are sent after it, in the same run.
- **F3 Quantity.** With `dosage_units` / `period_days` in the reference, each prescription line shows the
  prescribed quantity (units × days, at least 1); "Dispensed now" defaults to it and cannot exceed it.
  Without them (older server) there is no limit.
- **F4 Demographic edits.** `patient.update` sends `base_values` (the device value of each changed field before
  the edit; `estimated_age_*` have no downloaded value and are left out). Consecutive edits of one patient
  **chain** rather than merge: each later edit is its own record with `base_values` = the values after the
  previous edit, the same `base_modified`, and `depends_on` = the previous open edit. Records already queued
  are never rewritten. An accepted result's `modified` becomes the local base for the next edit.

## Development

```sh
cd apps/onerc_healthcare/frontend/outreach
yarn install
yarn dev            # http://localhost:5173/outreach/
```

`yarn dev` proxies `/api`, `/assets` and `/files` to the bench at `http://127.0.0.1:8004`, with
`Host: kalobeyei.localhost`. To use another site, set `BENCH_URL` and `BENCH_HOST`, e.g.
`BENCH_HOST=emr-test.localhost yarn dev`.

- **Boot info.** There is no server-rendered boot in dev; the app calls
  `onerc_healthcare.www.outreach.get_boot` instead.
- **Session cookie.** The proxy keeps the browser on `localhost:5173`, so the cookie is set there.
- **Service worker.** Not registered in dev. To test it, build and use the bench URL.

## Checks

```sh
yarn test           # Vitest (node + fake-indexeddb; app.test.tsx runs in jsdom)
yarn tsc --noEmit   # strict type check
yarn build          # type check + production build
yarn icons          # regenerate public/icons (red cross PNG/SVG)
```

## Layout

| Path | Contents |
|---|---|
| `src/lib/` | Logic without UI: crypto, db, session, package, outbox, sync, patients, visit, records, icd, vitals, protocol |
| `src/screens/` | One file per screen |
| `src/sw.ts` | The service worker (workbox precache plus app-shell navigation fallback; never caches `/api/`) |
