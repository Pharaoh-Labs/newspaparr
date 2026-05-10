# Troubleshooting

Common problems and how to fix them. For an overview of how a renewal works, see the README.

## Renewal failures

Every renewal attempt is recorded in the **Activity** tab with a final state. The five states and what they mean:

| State | What happened | What to do |
|---|---|---|
| `RENEWED` | Pass redeemed; expiration parsed from the response | Nothing — this is success |
| `NO_SESSION` | No NYT cookies on file for this account | Click the key icon and run the capture flow |
| `SESSION_EXPIRED` | Cookies were sent but NYT rejected them (typically ~30 days old) | Re-capture the session |
| `LIBRARY_AUTH_FAILED` | Library card / PIN was rejected by EZproxy | Check the card number and PIN; try logging in on the library website directly |
| `NETWORK_ERROR` | Couldn't reach the library or NYT | Library server may be down; check DNS / firewall; retry in a few minutes |
| `UNEXPECTED` | Reached an unrecognized end state | Open the Activity row to see the final URL and message; file an issue with that detail |

## Capture flow won't work

The one-time NYT capture opens an embedded Chromium that streams to your dashboard via noVNC on port 6100.

- **Black screen / "connecting…" forever** — port 6100 isn't reachable. If you're behind a reverse proxy, make sure WebSocket upgrades are forwarded to `:6100` as well as the dashboard on `:1851`.
- **Capture window opens but Chromium crashes** — usually a memory issue inside the container. Bump the container memory limit, or restart the container and retry.
- **"Save & close" does nothing** — make sure you're actually logged into NYT *and* on a page under `nytimes.com` before clicking save. The capture only stores cookies for that domain.
- **Subsequent renewals all return `SESSION_EXPIRED`** — sessions naturally age out (~30 days). Just re-capture.

## Library authentication

`LIBRARY_AUTH_FAILED` is by far the most common renewal failure. Things to check:

1. **PIN format** — some libraries use the last 4 digits of your phone number, your birthdate (MMDDYY or MMDDYYYY), or a custom PIN you set. Try the same value that works on the library's own website.
2. **EZproxy URL is current** — libraries occasionally change the redemption URL. Compare the URL configured under *Libraries* with the actual link your library shows for "NYT digital pass" today.
3. **Card not yet activated** — newly issued cards sometimes need 24h before EZproxy recognizes them.

## Scheduling

- **Renewals not running on time** — check that `TZ` is set in `docker-compose.yml`. Without it the scheduler defaults to UTC and renewals can appear hours off.
- **Renewals run every 24h regardless of expiration** — the scheduler reschedules around the next pass expiration parsed from the renewal response. If parsing fails it falls back to a 24h interval; the activity log will show a generic interval rather than a parsed date.

## Docker

- **Port 1851 in use** — change the host-side port in `docker-compose.yml` (e.g. `"8080:1851"`).
- **Permission errors on `data/`** — match `PUID` / `PGID` in compose to the owner of the bind-mounted directory.
- **Container restart loop** — `docker compose logs --tail=100 newspaparr` to see why. The most common causes are a malformed `APPRISE_URLS` value or a corrupted SQLite file (rare; restore from a backup of `data/`).

## Debug logging

Set `DEBUG_MODE=true` in your compose environment for verbose logging:

```yaml
environment:
  - DEBUG_MODE=true
```

Logs go to stdout (`docker compose logs -f newspaparr`) and to `data/logs/`.

## SECRET_KEY and at-rest encryption

Library-card passwords are encrypted with a Fernet key derived from `SECRET_KEY`. If `data/secret_key` is lost or `SECRET_KEY` is rotated, every stored library password becomes unreadable — you'll need to re-enter them. Include `data/secret_key` in any backup of `data/`.

## Inspecting the database

```bash
docker compose exec newspaparr python -c "
from app import app
from models import Account
with app.app_context():
    for a in Account.query.all():
        print(f'{a.id}: {a.name} ({a.library.name if a.library else \"-\"})')
"
```

## Getting help

If none of the above fixes it, open an issue at <https://github.com/Pharaoh-Labs/newspaparr/issues> with:

- The Activity-row final state and message
- Relevant lines from `docker compose logs newspaparr`
- The version (visible in the dashboard footer)
- Whether you're behind a reverse proxy
