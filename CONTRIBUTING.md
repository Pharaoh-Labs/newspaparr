# Contributing to Newspaparr

PRs and issues welcome. This file is short on purpose — read the README for what the project does, then come back for the workflow.

## Reporting issues

Before opening one, check if it's already filed. When you do open one, include:

- The Activity-row final state from the dashboard (`RENEWED`, `LIBRARY_AUTH_FAILED`, etc.)
- Relevant `docker compose logs newspaparr` output
- The version (footer of the dashboard) and how you're running it (Docker, behind a reverse proxy, etc.)
- Steps to reproduce, if applicable

For NYT-side flow regressions (NYT changes their HTML and the renewal stops detecting success), include the final URL and a snippet of the response if you can capture it.

## Submitting code

```bash
git clone https://github.com/YOUR_USERNAME/newspaparr.git
cd newspaparr
git checkout -b fix/short-description
# …make changes…
git commit -m "fix: short description"
git push origin fix/short-description
# …open a PR against main…
```

Conventional-commit-ish prefixes (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`) are appreciated but not enforced.

Keep PRs focused. One logical change per PR is much easier to review than a grab bag.

## Local development

```bash
./dev.sh
```

Creates a `.venv` on first run and starts gunicorn with `--reload` on `:1851`. Needs Python 3.13.

If you're editing Tailwind classes:

```bash
./scripts/build-css.sh
```

Rebuilds `static/css/app.css`. The script auto-fetches the standalone tailwindcss binary on first run (no npm).

## Testing

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

The full suite is intentionally small — smoke tests around the at-rest encryption, capture-session lifecycle, and a handful of route checks. Add tests when you fix a bug or add a non-trivial code path. End-to-end NYT renewal tests require a real library card and aren't part of CI.

## Architecture

See the **Architecture (code map)** section of the README. The short version:

- `app.py` is Flask routes only
- `renewer.py` is the HTTP-only renewal flow (no browser at renewal time)
- `capture_session.py` runs Xvfb + Chromium + x11vnc + websockify for the one-time login capture
- `cookie_jar.py` decrypts Chromium's cookie store
- `secrets_at_rest.py` Fernet-encrypts library passwords with a `SECRET_KEY`-derived key
- `scheduler.py` / `helpers.py` wrap APScheduler
- `models.py` / `forms.py` / `extensions.py` are SQLAlchemy / WTForms

The project deliberately avoids Selenium, headless browsers, and CAPTCHA solvers at renewal time — the cookie-bridge approach is the design choice. PRs that re-introduce automation frameworks for renewals will likely be declined unless they're the only viable path for a specific failure mode.

## Style

- Python 3.13, PEP 8, type hints where they help
- Keep diffs minimal; don't reformat surrounding code in a feature PR
- No new dependencies without a clear reason — the runtime image is small on purpose

## Code of conduct

Be respectful. Assume good faith. We're a small project; one toxic interaction is one too many.
