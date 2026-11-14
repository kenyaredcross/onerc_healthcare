# KRCS Outreach (offline PWA)

React 18 + TypeScript + Vite app for outreach clinics (Phase 4, SDD §5.6). The wire contract with the
server is `apps/onerc_healthcare/docs/OUTREACH_PROTOCOL.md`; every payload type is in `src/lib/protocol.ts`.

## Build and serve (production)

- `bench build --app onerc_healthcare` runs the app root `yarn build`, which runs
  `yarn install --frozen-lockfile && yarn build` here.
- Output goes to `onerc_healthcare/public/outreach/` (git-ignored), served at
  `/assets/onerc_healthcare/outreach/`.
- `www/outreach.py` renders `/outreach` and `/outreach/<path>`. It injects the hashed entry from
  `.vite/manifest.json` and `window.__OUTREACH_BOOT__` (CSRF token, site timezone, user).
- The same page serves `/outreach/sw.js`: the built service worker, with `Content-Type:
  application/javascript`, `Cache-Control: no-cache` and `Service-Worker-Allowed: /outreach/`.

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
