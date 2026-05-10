"""Extract cookies from a captured Chrome profile so the renewal flow can
launch a fresh Chrome (no --user-data-dir, no profile-launch fragility) and
inject the captured session/DataDome trust state directly via add_cookie."""

import hashlib
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


def extract_cookies(account_id: int, newspaper_type: str = 'nyt') -> List[dict]:
    """Return a list of selenium-shaped cookie dicts pulled from the captured
    profile for this account, restricted to the relevant newspaper hosts."""
    if not profile_exists(account_id):
        return []
    src_root = profile_dir_for(account_id)
    candidates = [
        os.path.join(src_root, 'Default', 'Network', 'Cookies'),
        os.path.join(src_root, 'Default', 'Cookies'),
    ]
    db_path = next((p for p in candidates if os.path.isfile(p)), None)
    if not db_path:
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
