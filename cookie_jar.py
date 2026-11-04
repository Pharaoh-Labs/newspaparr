"""Extract cookies from a captured Chrome profile so the renewal flow can
launch a fresh Chrome (no --user-data-dir, no profile-launch fragility) and
inject the captured session/DataDome trust state directly via add_cookie."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from typing import Iterable, List, Optional

from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from capture_session import profile_dir_for, profile_exists


# Linux Chrome (no keyring) derives its v10 key from "peanuts" with a fixed salt
# and 1 round of PBKDF2-SHA1, key length 16.  When a keyring IS available the
# key is random and stored in libsecret/KWallet; that path needs platform glue
# we don't ship yet — but in the docker container and on this host with no
# keyring, the peanuts derivation is what Chrome actually used to encrypt these
# cookies, so it's what we need to decrypt them.
def _v10_key(password: bytes = b'peanuts') -> bytes:
    kdf = PBKDF2HMAC(algorithm=_hashes.SHA1(), length=16,
                     salt=b'saltysalt', iterations=1)
    return kdf.derive(password)


def _decrypt_v10(blob: bytes, key: bytes, host_key: str) -> Optional[str]:
    """Decrypt a Chrome v10 cookie blob. Returns the plaintext or None.

    Chrome 80+ prepends SHA256(host_key) to the plaintext before encrypting
    so cookies can't be silently moved between sites; strip that if present."""
    if not blob.startswith(b'v10'):
        return None
    cipher = Cipher(algorithms.AES(key), modes.CBC(b' ' * 16))
    decryptor = cipher.decryptor()
    try:
        padded = decryptor.update(blob[3:]) + decryptor.finalize()
        pad_len = padded[-1]
        plaintext = padded[:-pad_len]
    except Exception:
        return None
    sha = hashlib.sha256(host_key.encode()).digest()
    if plaintext.startswith(sha):
        plaintext = plaintext[len(sha):]
    try:
        return plaintext.decode('utf-8')
    except UnicodeDecodeError:
        return None


# Hosts whose cookies actually matter for renewal — keeps add_cookie work bounded
# and avoids leaking unrelated cookies into our renewal session.
NEWSPAPER_HOSTS = {
    'nyt': ('nytimes.com', 'datadome.co'),
}


def _matches_any(host_key: str, suffixes: Iterable[str]) -> bool:
    h = (host_key or '').lstrip('.')
    return any(h == s or h.endswith('.' + s) for s in suffixes)


# --- Pasted cookies (manual capture path) ---
# Users can export their NYT cookies from their own browser (extension or
# devtools) and paste the JSON into the dashboard instead of using the
# noVNC capture flow. Stored per-account next to the Chrome profile.

def _pasted_path(account_id: int) -> str:
    return os.path.join(profile_dir_for(account_id), 'pasted_cookies.json')


# Cookie-Editor / EditThisCookie sameSite spellings → selenium/CDP spellings
_SAMESITE_MAP = {
    'no_restriction': 'None', 'none': 'None',
    'lax': 'Lax', 'strict': 'Strict',
}


def _normalize_pasted(raw: object, suffixes: Iterable[str]) -> List[dict]:
    """Accept a Cookie-Editor/EditThisCookie export (list of cookie objects,
    optionally wrapped in {"cookies": [...]}) and return our selenium shape,
    restricted to the relevant newspaper hosts."""
    if isinstance(raw, dict) and isinstance(raw.get('cookies'), list):
        raw = raw['cookies']
    if not isinstance(raw, list):
        raise ValueError("Expected a JSON array of cookies "
                         "(or an object with a 'cookies' array).")
    out: List[dict] = []
    for c in raw:
        if not isinstance(c, dict) or 'name' not in c or 'value' not in c:
            continue
        domain = c.get('domain') or ''
        if not _matches_any(domain, suffixes):
            continue
        entry = {
            'name': c['name'],
            'value': c['value'],
            'domain': domain,
            'path': c.get('path') or '/',
            'secure': bool(c.get('secure')),
            'httpOnly': bool(c.get('httpOnly', c.get('http_only'))),
        }
        # Cookie-Editor: expirationDate (float unix seconds); ours: expiry
        expiry = c.get('expirationDate', c.get('expiry'))
        if expiry:
            try:
                entry['expiry'] = int(float(expiry))
            except (TypeError, ValueError):
                pass
        samesite = c.get('sameSite')
        if isinstance(samesite, str):
            mapped = _SAMESITE_MAP.get(samesite.lower(), None)
            if mapped is None and samesite in ('None', 'Lax', 'Strict'):
                mapped = samesite
            if mapped:
                entry['sameSite'] = mapped
        out.append(entry)
    return out


def _parse_bare_nyts(text: str) -> List[dict]:
    """Parse a bare NYT-S value copied out of devtools (optionally with an
    'NYT-S=' prefix). Returns a one-cookie list in our selenium shape."""
    value = text.strip().strip('"').strip("'")
    if value.upper().startswith('NYT-S='):
        value = value[len('NYT-S='):]
    if any(ch.isspace() for ch in value):
        raise ValueError("That doesn't look like a cookie value — it contains "
                         "spaces. Copy just the NYT-S Value cell from devtools.")
    if len(value) < 20:
        raise ValueError("That looks too short to be an NYT-S value. Copy the "
                         "full Value cell of the NYT-S cookie from devtools.")
    return [{
        'name': 'NYT-S',
        'value': value,
        'domain': '.nytimes.com',
        'path': '/',
        'secure': True,
        'httpOnly': True,
    }]


def save_pasted_cookies(account_id: int, raw_text: str,
                        newspaper_type: str = 'nyt') -> List[dict]:
    """Parse and store a user paste: either a cookie JSON export or a bare
    NYT-S value from devtools. Returns the normalized list. Raises
    ValueError with a user-facing message on bad input."""
    raw_text = raw_text.strip()
    if raw_text and raw_text[0] not in '[{':
        cookies = _parse_bare_nyts(raw_text)
    else:
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise ValueError(f"That isn't valid JSON ({e.msg} at line {e.lineno}).")
        suffixes = NEWSPAPER_HOSTS.get(newspaper_type, NEWSPAPER_HOSTS['nyt'])
        cookies = _normalize_pasted(raw, suffixes)
        if not any(c['domain'].lstrip('.').endswith('nytimes.com') for c in cookies):
            raise ValueError("No nytimes.com cookies found in the paste — make "
                             "sure you export while on nytimes.com and logged in.")
        if not any(c['name'] == 'NYT-S' for c in cookies):
            raise ValueError("The NYT-S login cookie is missing from the paste. "
                             "Export ALL cookies for nytimes.com, or paste just "
                             "the NYT-S value from devtools.")
    with open(_pasted_path(account_id), 'w') as f:
        json.dump(cookies, f)
    return cookies


def load_pasted_cookies(account_id: int) -> List[dict]:
    path = _pasted_path(account_id)
    if not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def extract_cookies(account_id: int, newspaper_type: str = 'nyt') -> List[dict]:
    """Return a list of selenium-shaped cookie dicts for this account,
    restricted to the relevant newspaper hosts.

    Two possible sources: the captured Chrome profile's cookie DB (legacy
    noVNC flow) and a pasted-cookies JSON file (manual flow). They are
    merged per (name, domain), with whichever source was written more
    recently winning conflicts — a fresh NYT-S paste must beat a stale
    profile, but the profile's other cookies (e.g. DataDome trust state)
    still ride along."""
    profile_cookies = _extract_profile_cookies(account_id, newspaper_type)
    pasted = load_pasted_cookies(account_id)
    if not pasted:
        return profile_cookies
    if not profile_cookies:
        return pasted

    src_root = profile_dir_for(account_id)
    db_path = next((p for p in [
        os.path.join(src_root, 'Default', 'Network', 'Cookies'),
        os.path.join(src_root, 'Default', 'Cookies'),
    ] if os.path.isfile(p)), None)
    paste_newer = (db_path is None or
                   os.path.getmtime(_pasted_path(account_id)) >= os.path.getmtime(db_path))

    def key(c):
        return (c['name'], (c.get('domain') or '').lstrip('.'))

    older, newer = ((profile_cookies, pasted) if paste_newer
                    else (pasted, profile_cookies))
    merged = {key(c): c for c in older}
    merged.update({key(c): c for c in newer})
    return list(merged.values())


def _extract_profile_cookies(account_id: int, newspaper_type: str = 'nyt') -> List[dict]:
    """Cookies from the captured Chrome profile's SQLite DB, or []."""
    src_root = profile_dir_for(account_id)
    candidates = [
        os.path.join(src_root, 'Default', 'Network', 'Cookies'),
        os.path.join(src_root, 'Default', 'Cookies'),
    ]
    db_path = next((p for p in candidates if os.path.isfile(p)), None)

    if not profile_exists(account_id) or not db_path:
        return []

    suffixes = NEWSPAPER_HOSTS.get(newspaper_type, NEWSPAPER_HOSTS['nyt'])
    key = _v10_key()

    # Copy the DB because Chrome may hold a write-ahead lock on the original.
    tmp = tempfile.NamedTemporaryFile(prefix='nwspr-cookies-', suffix='.db',
                                       delete=False).name
    shutil.copy2(db_path, tmp)
    cookies: List[dict] = []
    try:
        conn = sqlite3.connect(tmp)
        for row in conn.execute(
            "SELECT host_key, name, value, encrypted_value, path, "
            "expires_utc, is_secure, is_httponly, samesite "
            "FROM cookies"
        ):
            host_key, name, value, enc, path, expires_utc, secure, http_only, samesite = row
            if not _matches_any(host_key, suffixes):
                continue
            cookie_value = value
            if not cookie_value and enc:
                decrypted = _decrypt_v10(enc, key, host_key)
                if decrypted is None:
                    continue
                cookie_value = decrypted
            entry = {
                'name': name,
                'value': cookie_value,
                'domain': host_key,
                'path': path or '/',
                'secure': bool(secure),
                'httpOnly': bool(http_only),
            }
            # Chrome stores expires_utc as microseconds since 1601-01-01.
            # Selenium wants seconds since 1970-01-01. Skip session cookies
            # (expires_utc == 0) — selenium treats no-expiry as session anyway.
            if expires_utc and expires_utc > 0:
                # 11644473600 seconds between 1601-01-01 and 1970-01-01.
                expiry = int(expires_utc / 1_000_000) - 11644473600
                if expiry > 0:
                    entry['expiry'] = expiry
            samesite_map = {0: 'None', 1: 'Lax', 2: 'Strict'}
            if samesite in samesite_map:
                entry['sameSite'] = samesite_map[samesite]
            cookies.append(entry)
        conn.close()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return cookies
