[← back to README](../README.md)

## Remote access (phone / second machine, via Tailscale)

This app is local-first by default — nothing here is required to use it on
the machine it runs on. This doc is for reaching that same instance from a
phone or another computer, without deploying it anywhere public.

**REQUIRED before any non-localhost exposure**: enable `JWT_SECRET` or
`API_AUTH_TOKEN` (see the README's Security/Deployment guidance) *before*
your phone or any other device can reach this app's address. Every default
in this app assumes a single trusted user on localhost; the moment the
address is reachable from another device, that assumption is gone —
anyone who can reach it can read your conversations, run paid model calls
against your budget, and change your settings. There is no separate
read-only mode.

### Install [Tailscale](https://tailscale.com/) on both devices

Tailscale creates a private, encrypted network (a "tailnet") between your
devices, addressed by a stable `100.x.y.z` IP or a `<device>.<tailnet>.ts.net`
hostname — no port forwarding, no public exposure, no separate VPN
configuration. Install it on the machine running this app and on your
phone/laptop, sign both into the same tailnet, and they can reach each
other directly.

### Option A (recommended): `tailscale serve`

Build the frontend once, then keep uvicorn bound to `127.0.0.1` exactly as
today (`start-app.bat`, or `uvicorn app.main:app --port 8000`), and front it
with Tailscale's own HTTPS reverse proxy:

**`start-remote.bat` does everything in this section for you** — the auth
check below, the build, the backend, the tunnel, and printing the URL to
open on the phone. The manual steps are kept here because they are what the
script runs, and they are what you need on a non-Windows host:

```bash
cd frontend && npm run build
tailscale serve --bg 8000
```

This serves the **whole app** — UI and API both — at
`https://<device>.<tailnet>.ts.net`: the backend detects `frontend/dist` and
serves it directly (static assets, with an `index.html` fallback for
client-side routes), so a single tunnel is enough. This is also what makes
the app usable from older mobile browsers (e.g. iOS 15 Safari) that show a
blank page against the untranspiled Vite dev server — the build step
transpiles for them (`frontend/vite.config.ts`'s `build.target`).
Reachable only from other devices on your tailnet, over HTTPS, with a real
certificate Tailscale manages for you. The app process itself never binds
beyond localhost, so this app's own "exposed without auth" startup check
(below) correctly stays silent — `tailscale serve` is the actual trust
boundary here, not this app. Run `tailscale serve --https=8000 off` to stop
serving.

**Rebuild after frontend changes**: `frontend/dist` isn't rebuilt
automatically — re-run `npm run build` any time frontend source changes,
then the running backend will pick up the new files on the next request (no
restart needed).

If `frontend/dist` doesn't exist yet, `/` falls back to the same plain JSON
identity ping it always returned, and the UI isn't reachable through this
tunnel — build first.

#### Reaching the Vite dev server instead (modern browsers only)

For active frontend development you'll usually still run `npm run dev` and
want to reach *that* from your phone instead of a stale build. The same
`tailscale serve` approach works against port `5173`:

```bash
tailscale serve --bg 5173
```

`vite dev`/`vite preview` reject any request whose `Host` header they don't
recognize (DNS-rebinding protection); `tailscale serve` proxies to
`127.0.0.1:5173` (so the socket binding stays localhost — nothing new is
exposed) but forwards the original `desktop-name.tailnet.ts.net` `Host`
header, which Vite would otherwise reject with a blank/blocked page.
`frontend/vite.config.ts` allows exactly the `.ts.net` suffix
(`allowedHosts: [".ts.net"]`) so tailnet hostnames pass this check — nothing
else does. Run `tailscale serve --https=5173 off` to stop serving it. This
route serves untranspiled dev code, so it only works on modern browsers —
older mobile Safari needs the built-and-served-by-the-backend route above.

### Option B: bind uvicorn directly to the tailnet IP

If you'd rather not use `tailscale serve`, bind uvicorn straight to the
tailnet interface:

```bash
# find your tailnet IP first: `tailscale ip -4`
set BIND_HOST=100.x.y.z
set JWT_SECRET=<a-long-random-value>
venv\Scripts\python.exe -m uvicorn app.main:app --host %BIND_HOST% --port 8000
```

`BIND_HOST` isn't read by uvicorn itself (uvicorn's `--host` flag is what
actually binds the socket) — set both to the same address. `BIND_HOST` is
purely this app's own signal so it can warn you at startup if it's bound
beyond localhost with no auth configured (see below); uvicorn's `--host`
does the real work. This option exposes exactly the port you bind, still
only to your tailnet (Tailscale itself is the isolation, same as Option A)
— but unlike `tailscale serve`, there's no TLS termination unless you add
one yourself, and no reason to prefer it unless Option A doesn't fit your
setup.

Either way, `curl https://<device>.<tailnet>.ts.net/health` (Option A) or
`curl http://100.x.y.z:8000/health` (Option B) from your phone confirms
it's reachable before you rely on it.

### The mobile layout already exists

The frontend's responsive layout (sidebar collapses, composer and message
list adapt to a phone-width viewport) was already built — see
[docs/development.md](development.md)'s design notes. Nothing extra is
needed on the frontend side to use this app from a phone's browser; this
doc is only about how the phone reaches the backend at all.

### Add to Home Screen

The frontend ships a minimal PWA manifest (`frontend/public/manifest.webmanifest`)
and icon set, so once you've loaded the app in your phone's browser over
your tailnet address, "Add to Home Screen" (Safari: Share → Add to Home
Screen; Chrome: menu → Add to Home screen / Install app) installs it as a
standalone app icon — no browser chrome, launches straight into the app.
This is a thin convenience layer (an icon and a `start_url`), not a real
offline-capable PWA — there's no service worker, so the app still needs a
live connection to reach the backend.

### The safety nudge

If `BIND_HOST` is set to anything other than `127.0.0.1`/`localhost`/`::1`
and neither `API_AUTH_TOKEN` nor `JWT_SECRET` is configured, this app logs
one loud warning at startup (`startup.exposed_without_auth`, alongside the
other consolidated startup warnings — see `app/main.py`). It can't stop
you from running unauthenticated on a tailnet address, but it makes sure
you can't miss that you are. This only ever fires for Option B above —
`tailscale serve` (Option A) never sets `BIND_HOST`, since uvicorn itself
never leaves localhost in that setup.
