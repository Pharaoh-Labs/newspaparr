"""Symmetric at-rest encryption for sensitive Account columns.

Library card credentials are stored in `data/newspaparr.db`. For a
self-hosted single-user app that's "fine" until someone rsyncs the data
directory to an unencrypted backup, hands it to a teammate, etc. So we
encrypt with Fernet (AES-128-CBC + HMAC-SHA256) keyed off the app's
SECRET_KEY.

Properties:
- Fernet key is derived from SECRET_KEY via PBKDF2-HMAC-SHA256 with a fixed
  salt. Same SECRET_KEY → same Fernet key, so encrypted values survive
  restarts. The salt is constant because SECRET_KEY is already a 48-byte
  url-safe random token.
- Rotating SECRET_KEY invalidates every encrypted column. This is a feature
  (key rotation = revocation) but it means losing data/secret_key locks the
  user out of stored creds. The README warns about this.
- The TypeDecorator transparently encrypts on write, decrypts on read. If a
  value fails to decrypt (InvalidToken) it's returned as-is — that's the
  signal for the boot-time migration to re-encrypt legacy plaintext rows.
"""
import base64
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy import String, TypeDecorator

logger = logging.getLogger(__name__)

_SALT = b'newspaparr-account-creds-v1'
_ITERATIONS = 200_000


@lru_cache(maxsize=4)
def _cipher_for(secret_key: str) -> Fernet:
    """Cache the Fernet object — PBKDF2 with 200k iterations isn't free.
    Keyed by the SECRET_KEY string itself so a key rotation produces a
    fresh cipher."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=_ITERATIONS,
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret_key.encode()))
    return Fernet(key)


def _get_secret_key() -> str:
    """Pull SECRET_KEY off the live Flask app at call time. Imported lazily
    to avoid a circular import (app.py imports this module)."""
    from flask import current_app
    return current_app.config['SECRET_KEY']


def encrypt(plaintext: str | None) -> str | None:
    if plaintext is None or plaintext == '':
        return plaintext
    return _cipher_for(_get_secret_key()).encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str | None) -> str | None:
    if ciphertext is None or ciphertext == '':
        return ciphertext
    try:
        return _cipher_for(_get_secret_key()).decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        # Legacy plaintext row, or wrong SECRET_KEY. Return as-is so the
        # boot-time migration can re-encrypt; renewals on a row that's
        # actually encrypted with a *different* key will fail loudly when
        # the library auth POST rejects the bogus password.
        return ciphertext


def is_encrypted(value: str | None) -> bool:
    """True iff the value is a valid Fernet token under our current key."""
    if not value:
        return False
    try:
        _cipher_for(_get_secret_key()).decrypt(value.encode())
        return True
    except InvalidToken:
        return False


class EncryptedString(TypeDecorator):
    """SQLAlchemy column type that encrypts on write, decrypts on read.

    Drop-in replacement for db.String() — existing rows containing plaintext
    are returned as-is by decrypt() and get re-encrypted by the boot-time
    migration in migrate_plaintext_rows()."""
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)


def migrate_plaintext_rows(db, Account) -> int:
    """One-shot: re-encrypt any Account.library_password rows that aren't
    already valid Fernet tokens. Idempotent — safe to call on every boot.
    Returns the number of rows migrated.

    Uses raw UPDATEs rather than the ORM because re-assigning the same
    plaintext to an attribute is a no-op from SQLAlchemy's POV — it doesn't
    flush the change, so process_bind_param never runs."""
    migrated = 0
    rows = db.session.execute(
        db.text("SELECT id, library_password FROM account")
    ).fetchall()
    for row_id, raw in rows:
        if raw is None or raw == '' or is_encrypted(raw):
            continue
        # Encrypt the plaintext directly and write the ciphertext via UPDATE
        ciphertext = _cipher_for(_get_secret_key()).encrypt(raw.encode()).decode()
        db.session.execute(
            db.text("UPDATE account SET library_password = :p WHERE id = :id"),
            {'p': ciphertext, 'id': row_id},
        )
        migrated += 1
    if migrated:
        db.session.commit()
        logger.info("secrets_at_rest: migrated %d plaintext password row(s)", migrated)
    return migrated
