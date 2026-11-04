# Newspaparr

Self-hosted automation for keeping your library-provided **New York Times** digital pass active. One capture of your NYT session, then daily renewals that finish in about a second each.

![Version](https://img.shields.io/badge/version-1.1.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Docker](https://img.shields.io/badge/docker-ready-blue)

## What it does

Many public libraries offer free NYT digital access through OCLC EZproxy redemption pages. Each redemption gives you 24–72 hours, so without automation you'd be doing a manual login every day. Newspaparr does the renewal for you on a schedule.

A renewal is two short steps: Newspaparr POSTs your library card credentials to your library's EZproxy endpoint and follows the redirect chain into NYT over plain HTTP, then opens the final redemption URL in a headless Chromium carrying your captured session so NYT's own page fires the redemption. No CAPTCHA solver, no per-renewal login. A renewal takes a few seconds.

The hard part is the *initial* NYT login — NYT actively detects automation. Newspaparr solves this with a **one-time capture**: you log into NYT inside an embedded Chrome that streams to your dashboard via noVNC. The captured cookies are then replayed by every subsequent renewal. Sessions typically last ~30 days; re-capture when renewals start failing.

## Features

- **Lightweight renewals** — a few seconds per account; the browser only opens the final redemption page
- **Proxy egress (optional)** — pin each account to a proxy lane so your home IP never touches the library or NYT
- **Library-card auth only** — your NYT password is never stored, only captured cookies
- **One-time session capture** — log into NYT once via an in-dashboard browser
- **Apprise notifications** — ~80 services (Discord, ntfy, Telegram, Slack, mailto, …) on renewal failure or recovery
- **Multi-account** — capture each household member's NYT session independently
- **Scheduling** — renewals rescheduled around the next pass expiration with a random delay, plus optional quiet hours, so nothing fires at the same second every day
- **Activity log** — every attempt recorded with status, message, and duration

## Quick start

### Requirements

- Docker + Docker Compose
- A library card at a library that offers NYT digital access via EZproxy
- An NYT.com account (free, no paid subscription needed)

### Run it

```bash
# Grab the example compose file
curl -fsSL https://raw.githubusercontent.com/Pharaoh-Labs/newspaparr/main/docker-compose.example.yml -o docker-compose.yml

# Start
docker-compose up -d

# Open the dashboard
open http://localhost:1851
```

### First-time setup

1. **Add your library** (Libraries → Add library). Enter the EZproxy URL your library exposes for NYT redemption. Newspaparr ships defaults for OCLC/EZproxy; most libraries use this pattern.
2. **Add an account** (Accounts → Add account). Enter a name, your library card number, and PIN.
3. **Capture your NYT session.** Click the key icon next to the new account. A live Chrome window opens in your browser. Log into NYT normally, land on your account page, then click *Save & close*. Cookies are encrypted with `SECRET_KEY` and stored in `data/`.
4. **Done.** The first renewal runs immediately; subsequent renewals are scheduled around the pass expiration.

## Configuration

All configuration is environment variables in `docker-compose.yml`. Everything is optional except the basics.

```yaml
services:
  newspaparr:
    image: ghcr.io/pharaoh-labs/newspaparr:latest
    ports:
      - "1851:1851"   # Web dashboard
      - "6100:6100"   # noVNC bridge (only used during the capture flow)
    volumes:
      - ./data:/app/data
    environment:
      - TZ=America/New_York
      - PUID=1000
      - PGID=1000

      # Optional: notifications via Apprise.
      # https://github.com/caronc/apprise — supports ~80 services.
      # Comma-separated. Fires on renewal failure and on recovery from a
      # previous failure (not on every attempt).
      # - APPRISE_URLS=ntfy://ntfy.sh/your-topic,discord://webhook_id/webhook_token

      # Optional: verbose logging
      # - DEBUG_MODE=true

      # Optional: behind a reverse proxy that sets X-Forwarded-* headers
      # - BEHIND_PROXY=true
    restart: unless-stopped
```

`SECRET_KEY` is auto-generated and persisted to `data/secret_key` (mode 0600) on first boot. Set it explicitly via the env var if you want to manage it yourself.

### Renewal cadence

A pass only redeems once the previous one has lapsed, so the next renewal is scheduled *after* the expiry NYT reports. It is never a fixed offset: a renewal that fires exactly one minute after expiry, every day at the same second, is a bot signal on its own.

```yaml
    environment:
      # Random delay after the pass expires before the next renewal (minutes).
      # Defaults: 5 to 120. Set both equal for a fixed offset.
      # - RENEWAL_DELAY_MIN_MINUTES=5
      # - RENEWAL_DELAY_MAX_MINUTES=120
      # Optional: never renew inside this window (local TZ, may wrap midnight).
      # A run that lands inside is moved to a random point just after it ends.
      # - RENEWAL_QUIET_HOURS=00:30-07:00
```

Failed attempts retry on the same jittered cadence. The manual *Renew now* button runs immediately. `GET /api/status` shows the active cadence settings.

### Proxy egress (keep your home IP out of it)

NYT's bot defences (DataDome) score the IP the session was captured from and the IP each renewal comes from. If your home IP gets flagged, or you just do not want it associated with the automation, give Newspaparr a pool of HTTP proxies and it will route **everything** through them: the capture browser, the EZproxy chain, the headless redemption and the verification call. The host's own IP is never used once a lane is configured.

```yaml
    environment:
      # One or more HTTP(S) proxy URLs, comma or space separated ...
      - PROXY_URLS=http://user:pass@1.2.3.4:8080,http://user:pass@5.6.7.8:8080
      # ... and/or a file (default: data/proxies.txt). Webshare "direct" export
      # format (ip:port:user:pass per line), full URLs and # comments all work.
      # - PROXIES_FILE=/app/data/proxies.txt
      # Refuse to renew or capture at all when no lane is usable (default: true
      # as soon as any lane is configured, false otherwise).
      # - PROXY_REQUIRED=true
```

How lanes work:

- Each account is **pinned to one lane** the first time it captures or renews, and stays on it. The trust NYT grants during capture belongs to that exit IP, so capture and every renewal must share it. The lane is shown under the account name (`via 1.2.3.4:8080`) and in the activity log.
- **Rotate** (the arrows icon next to an account, shown when there is more than one lane) moves the account to the least-used other lane and sends you straight to re-capture. Do this when a lane gets blocked.
- A lane removed from the list is re-chosen at the next capture; a renewal on a vanished lane fails with `Egress blocked` instead of silently going direct.
- Static residential or ISP proxies work best. Rotating-IP proxies defeat the pin. SOCKS proxies are not supported.
- Already have a `proxies.txt` for another tool? Bind-mount it read-only and point `PROXIES_FILE` at it (see the compose example); the file is re-read on every use, so edits apply without a restart.
- Chromium cannot take proxy credentials on its command line, so Newspaparr runs a loopback-only forwarder per browser launch that adds them. WebRTC is restricted to proxied traffic so it cannot reveal the real IP.

> **Important:** library card passwords are encrypted at rest with a key derived from `SECRET_KEY` (Fernet, AES-128-CBC + HMAC-SHA256). If you lose or rotate `SECRET_KEY`, every stored library password becomes unreadable and you'll need to re-enter them. Keep `data/secret_key` in your backup set if you back up `data/`.

## How a renewal works

1. **Library auth.** Newspaparr GETs your library's EZproxy URL, parses the login form, and POSTs `{user, pass, url}`.
2. **Redirect chain.** httpx walks the redirects: library auth → EZproxy proxy URL → NYT, stopping just before the one-shot redemption URL so its token is not consumed.
3. **Cookie injection.** The captured NYT session cookies (`datadome`, `NYT-S`, etc.) are loaded onto the request so NYT recognizes you.
4. **Redemption.** A headless Chromium with the captured profile opens the redemption URL; NYT's page fires the redemption itself. The NYT data-layer is then queried for the real expiry date.
5. **Egress.** With proxy lanes configured, every one of those requests leaves through the account's pinned lane (see *Proxy egress*).
6. **State classification.** The final response is classified into one of:
   - `RENEWED` — server-side `isProvisionallyLoggedIn:true` flag or success copy detected
   - `NO_SESSION` — no captured cookies; capture an initial session
   - `SESSION_EXPIRED` — cookies present but rejected; re-capture
   - `LIBRARY_AUTH_FAILED` — library card / PIN wrong
   - `NETWORK_ERROR` — couldn't reach the library or NYT
   - `UNEXPECTED` — something else; details in the activity log

## Architecture (code map)

```
app.py              Flask routes only (~850 lines)
models.py           SQLAlchemy models (Library, Account, RenewalLog)
forms.py            WTForms classes
extensions.py       SQLAlchemy + CSRF singletons (avoids circular imports)
scheduler.py        APScheduler setup, manual triggers, reschedule logic
helpers.py          Renewal helpers (run, schedule, log) shared by routes + scheduler
renewer.py          The renewal flow: httpx EZproxy chain + headless-Chromium redemption + data-layer verify
egress.py           Proxy lanes (list parsing, per-account pin, loopback forwarder for Chromium)
cookie_jar.py       Linux Chrome v10 cookie decrypt (PBKDF2 'peanuts'/'saltysalt')
capture_session.py  Xvfb + Chromium + x11vnc + websockify lifecycle for the in-dashboard capture flow
secrets_at_rest.py  Fernet encryption of library passwords using a SECRET_KEY-derived key
notify.py           Apprise wrapper — fires on failure, on recovery
icons.py            Inline-SVG Heroicons helper (drops the Font Awesome CDN)
paths.py            Single source of truth for filesystem paths
templates/          Jinja templates, Tailwind classes
static/css/app.css  Built Tailwind bundle (no CDN at runtime)
scripts/build-css.sh    Re-build static/css/app.css after editing classes
```

## Activity & troubleshooting

- **Activity tab** in the dashboard shows every renewal attempt with status, duration, and the full final-page URL.
- **Manual renewal** — click the refresh icon next to any account. Useful right after capturing a session.
- **Debug logs** — `docker-compose logs -f newspaparr` or `data/logs/`.

Common failures:

| Symptom | Likely cause |
|---|---|
| `LIBRARY_AUTH_FAILED` | Card number / PIN wrong, or the library EZproxy URL is stale |
| `SESSION_EXPIRED` | NYT cookies aged out (~30 days). Re-capture the session. |
| `NO_SESSION` | Account was added but never had a session captured. Click the key icon. |
| `NETWORK_ERROR` | Library server is down, DNS / firewall blocked the request, or the proxy lane is unreachable |
| `Egress blocked` | Proxy lanes are required but none is usable for this account (see Proxy egress) |

## Development

```bash
git clone https://github.com/Pharaoh-Labs/newspaparr.git
cd newspaparr
./dev.sh           # creates .venv on first run, then gunicorn --reload on :1851
```

Editing Tailwind classes? Re-run `./scripts/build-css.sh` to rebuild `static/css/app.css`. The script auto-fetches the standalone tailwindcss binary on first run (no npm).

## What this is not

- **Not a paywall bypass.** It only renews access that your library has *already* given you.
- **Not a NYT scraper.** It just keeps a normal authenticated session alive.
- **Not affiliated** with The New York Times or any library system.

## Versioning

- **v1.1.0** — current. HTTP-only renewer, NYT-only, captured-session auth, Apprise notifications, static Tailwind, inline SVG icons.
- **v1.0.0** — first cookie-bridge release; WSJ retired.
- **v0.x** — selenium + undetected-chromedriver + CAPTCHA solving era.

See `CHANGELOG.md` (when present) for full history.

## License

MIT. See [LICENSE](LICENSE).
