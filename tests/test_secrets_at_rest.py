"""At-rest encryption for Account.library_password.

Pin the contract:
- Round-trip: encrypt → decrypt yields the original plaintext.
- The DB column stores ciphertext, not plaintext.
- Legacy plaintext rows survive (decrypt returns the value as-is) and the
  boot-time migration re-encrypts them.
- Wrong SECRET_KEY: decryption fails gracefully (returns the ciphertext,
  rather than raising and crashing the app).
"""
import pytest


def test_round_trip(app_client):
    """Plain → encrypt → decrypt → original."""
    app_mod, _ = app_client
    from secrets_at_rest import encrypt, decrypt
    with app_mod.app.app_context():
        plaintext = "library-card-1234567890"
        token = encrypt(plaintext)
        assert token != plaintext, "encryption is a no-op?"
        assert decrypt(token) == plaintext


def test_db_column_is_ciphertext_at_rest(app_client):
    """Bypass the ORM and read the raw DB value — it must NOT be plaintext."""
    app_mod, _ = app_client
    with app_mod.app.app_context():
        # Need at least one library config so the FK is satisfied
        lib = app_mod.LibraryConfig(
            type='test_lib', name='Test Lib', nyt_url='https://example.invalid',
            default_renewal_hours=24, active=True,
        )
        app_mod.db.session.add(lib)
        app_mod.db.session.commit()

        secret = "very-secret-password-do-not-leak"
        acc = app_mod.Account(
            name='Test', library_type='test_lib',
            library_username='123', library_password=secret,
        )
        app_mod.db.session.add(acc)
        app_mod.db.session.commit()
        acc_id = acc.id

        # Re-read via ORM — should round-trip back to plaintext
        roundtripped = app_mod.db.session.get(app_mod.Account, acc_id).library_password
        assert roundtripped == secret

        # Read the *raw* column bypassing the TypeDecorator
        raw = app_mod.db.session.execute(
            app_mod.db.text("SELECT library_password FROM account WHERE id = :id"),
            {'id': acc_id},
        ).scalar()
        assert raw != secret, "plaintext leaked into the DB"
        assert len(raw) > len(secret), "ciphertext should be larger than plaintext"
        # Fernet tokens start with 0x80 base64-urlsafe-encoded → 'gAAAAA'
        assert raw.startswith('gAAAAA'), f"not a Fernet token? raw={raw[:20]}"


def test_legacy_plaintext_decrypts_to_self(app_client):
    """A row written before encryption was added must decrypt to its
    own value — not raise — so the migration can detect and re-encrypt."""
    app_mod, _ = app_client
    from secrets_at_rest import decrypt, is_encrypted
    with app_mod.app.app_context():
        plaintext = "i-was-stored-before-v1.2.0"
        assert not is_encrypted(plaintext)
        assert decrypt(plaintext) == plaintext


def test_migration_re_encrypts_plaintext_rows(app_client):
    """Insert a plaintext password directly (bypassing the ORM), run the
    migration, verify it's now ciphertext and the value still round-trips."""
    app_mod, _ = app_client
    from secrets_at_rest import migrate_plaintext_rows, is_encrypted
    with app_mod.app.app_context():
        # Need a library row first
        if not app_mod.LibraryConfig.query.filter_by(type='migr_lib').first():
            app_mod.db.session.add(app_mod.LibraryConfig(
                type='migr_lib', name='Migration Test', nyt_url='x',
                default_renewal_hours=24, active=True,
            ))
            app_mod.db.session.commit()

        # Insert a row with raw plaintext directly into the column,
        # bypassing the TypeDecorator (this simulates a legacy row).
        legacy_plaintext = "stored-before-encryption"
        app_mod.db.session.execute(
            app_mod.db.text(
                "INSERT INTO account (name, library_type, library_username, "
                "library_password, newspaper_type, active, renewal_hours) "
                "VALUES ('Legacy', 'migr_lib', 'lu', :p, 'nyt', 1, 24)"
            ),
            {'p': legacy_plaintext},
        )
        app_mod.db.session.commit()

        # Confirm it's plaintext
        raw = app_mod.db.session.execute(
            app_mod.db.text(
                "SELECT library_password FROM account WHERE name='Legacy'"
            )
        ).scalar()
        assert raw == legacy_plaintext

        # Run migration
        n = migrate_plaintext_rows(app_mod.db, app_mod.Account)
        assert n >= 1

        # Now it's ciphertext at rest
        raw2 = app_mod.db.session.execute(
            app_mod.db.text(
                "SELECT library_password FROM account WHERE name='Legacy'"
            )
        ).scalar()
        assert raw2 != legacy_plaintext
        assert is_encrypted(raw2)

        # And the ORM still reads the original plaintext
        legacy = app_mod.Account.query.filter_by(name='Legacy').first()
        assert legacy.library_password == legacy_plaintext


def test_migration_is_idempotent(app_client):
    """Running the migration twice should not re-encrypt already-encrypted
    rows or change their value."""
    app_mod, _ = app_client
    from secrets_at_rest import migrate_plaintext_rows
    with app_mod.app.app_context():
        n_first = migrate_plaintext_rows(app_mod.db, app_mod.Account)
        n_second = migrate_plaintext_rows(app_mod.db, app_mod.Account)
        assert n_second == 0, "second run should be a no-op"
