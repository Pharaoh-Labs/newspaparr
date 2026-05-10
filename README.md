# Newspaparr

Self-hosted automation for keeping your library-provided **New York Times** digital pass active. One capture of your NYT session, then daily renewals that finish in about a second each.

![Version](https://img.shields.io/badge/version-1.1.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Docker](https://img.shields.io/badge/docker-ready-blue)

## What it does

Many public libraries offer free NYT digital access through OCLC EZproxy redemption pages. Each redemption gives you 24–72 hours, so without automation you'd be doing a manual login every day. Newspaparr does the renewal for you on a schedule.

The renewal flow is **HTTP-only** — Newspaparr POSTs your library card credentials to your library's EZproxy endpoint, follows the redirect chain into NYT, and reads the activation response. No headless browser, no proxy, no CAPTCHA solver. A single renewal takes ~1 second.

The hard part is the *initial* NYT login — NYT actively detects automation. Newspaparr solves this with a **one-time capture**: you log into NYT inside an embedded Chrome that streams to your dashboard via noVNC. The captured cookies are then replayed by every subsequent renewal. Sessions typically last ~30 days; re-capture when renewals start failing.

## Features

- **HTTP-only renewals** — ~1s per account, no browser at renewal time
- **Library-card auth only** — your NYT password is never stored, only captured cookies
- **One-time session capture** — log into NYT once via an in-dashboard browser
- **Apprise notifications** — ~80 services (Discord, ntfy, Telegram, Slack, mailto, …) on renewal failure or recovery
- **Multi-account** — capture each household member's NYT session independently
- **Scheduling** — renewals automatically rescheduled around the next pass expiration
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

> **Important:** library card passwords are encrypted at rest with a key derived from `SECRET_KEY` (Fernet, AES-128-CBC + HMAC-SHA256). If you lose or rotate `SECRET_KEY`, every stored library password becomes unreadable and you'll need to re-enter them. Keep `data/secret_key` in your backup set if you back up `data/`.

## How a renewal works

1. **Library auth.** Newspaparr GETs your library's EZproxy URL, parses the login form, and POSTs `{user, pass, url}`.
2. **Redirect chain.** httpx follows the redirects: library auth → EZproxy proxy URL → NYT redemption page.
3. **Cookie injection.** The captured NYT session cookies (`datadome`, `NYT-S`, etc.) are loaded onto the request so NYT recognizes you.
4. **State classification.** The final response is classified into one of:
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
renewer.py          The HTTP-only renewal flow (~240 lines, replaces the old selenium pipeline)
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
| `NETWORK_ERROR` | Library server is down, or DNS / firewall blocked the request |

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
