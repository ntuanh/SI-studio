"""On-disk secret store.

Layout under SECRETS_DIR:
    <key_ref>.pem        SSH private keys
    credentials.json     {ref: {"password": "<fernet token>", "enc": "fernet"}}
    .master.key          Fernet key protecting credentials.json  (chmod 600)

Passwords and key passphrases are encrypted at rest with Fernet (AES-128-CBC +
HMAC). The master key is read from `SECRET_ENCRYPTION_KEY` when set, otherwise
generated once into `.master.key`. Values written before encryption was added
are still readable and are re-encrypted the next time they are written.

Nothing in here is ever serialized into an API response -- the routers only
handle the *reference* (`key_ref` / `password_ref`) or the fact that a secret is
on file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

log = logging.getLogger(__name__)

_SAFE_REF = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CRED_FILE = "credentials.json"
_MASTER_KEY_FILE = ".master.key"

#: Marker stored alongside a value so legacy plaintext stays readable.
_ENC_FERNET = "fernet"


class SecretError(ValueError):
    pass


def _root() -> Path:
    p = settings.secrets_path
    p.mkdir(parents=True, exist_ok=True)
    return p


def _restrict(path: Path) -> None:
    """Best effort 0600. No-op semantics on Windows, which has no POSIX mode."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - platform dependent
        log.debug("could not chmod %s", path)


def validate_ref(key_ref: str) -> str:
    """Reject anything that could escape SECRETS_DIR."""
    ref = (key_ref or "").strip()
    if not ref:
        raise SecretError("key_ref is empty")
    if not _SAFE_REF.match(ref) or ".." in ref:
        raise SecretError(f"invalid key_ref: {key_ref!r}")
    return ref


# ------------------------------------------------------------------ SSH keys
def key_path(key_ref: str) -> Path:
    ref = validate_ref(key_ref)
    candidate = (_root() / (ref if ref.endswith(".pem") else f"{ref}.pem")).resolve()
    if _root().resolve() not in candidate.parents:
        raise SecretError(f"key_ref escapes secrets dir: {key_ref!r}")
    return candidate


def key_exists(key_ref: str) -> bool:
    try:
        return key_path(key_ref).is_file()
    except SecretError:
        return False


def save_key(key_ref: str, pem: str | bytes) -> Path:
    path = key_path(key_ref)
    data = pem.encode() if isinstance(pem, str) else pem
    path.write_bytes(data)
    _restrict(path)
    return path


def delete_key(key_ref: str) -> bool:
    path = key_path(key_ref)
    if path.is_file():
        path.unlink()
        return True
    return False


def list_keys() -> list[str]:
    return sorted(p.stem for p in _root().glob("*.pem"))


# ----------------------------------------------------------------- encryption
_fernet: Fernet | None = None


def _cipher() -> Fernet:
    """Load (or mint) the master key protecting `credentials.json`."""
    global _fernet
    if _fernet is not None:
        return _fernet

    env_key = (os.environ.get("SECRET_ENCRYPTION_KEY") or "").strip()
    if env_key:
        _fernet = Fernet(env_key.encode())
        return _fernet

    path = _root() / _MASTER_KEY_FILE
    if path.is_file():
        key = path.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        path.write_bytes(key)
        _restrict(path)
        log.info("generated a new secret-store master key at %s", path)
    _fernet = Fernet(key)
    return _fernet


def _encrypt(value: str) -> str:
    return _cipher().encrypt(value.encode()).decode()


def _decrypt(value: str, ref: str, field: str) -> str | None:
    try:
        return _cipher().decrypt(value.encode()).decode()
    except InvalidToken:
        # Wrong/rotated master key -- refuse rather than returning garbage.
        log.error(
            "cannot decrypt %s.%s: master key does not match. Re-save the secret.",
            ref, field,
        )
        return None


# ------------------------------------------------------------------ passwords
def _cred_path() -> Path:
    return _root() / _CRED_FILE


def _read_creds() -> dict[str, dict[str, str]]:
    path = _cred_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("credentials store unreadable (%s); treating as empty", exc)
        return {}


def _write_creds(data: dict[str, dict[str, str]]) -> None:
    path = _cred_path()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _restrict(path)


def set_secret(ref: str, field: str, value: str) -> None:
    """Store one encrypted value under `ref`. An empty value clears the slot."""
    data = _read_creds()
    if value:
        slot = data.setdefault(ref, {})
        slot[field] = _encrypt(value)
        slot["enc"] = _ENC_FERNET
    else:
        slot = data.get(ref)
        if slot is not None:
            slot.pop(field, None)
            if not [k for k in slot if k != "enc"]:
                data.pop(ref, None)
    _write_creds(data)


def get_secret(ref: str, field: str) -> str | None:
    slot = _read_creds().get(ref) or {}
    raw = slot.get(field)
    if raw is None:
        return None
    if slot.get("enc") == _ENC_FERNET:
        return _decrypt(raw, ref, field)
    return raw  # written before encryption was introduced


def has_secret(ref: str, field: str) -> bool:
    """True when a value is on file, without decrypting it."""
    return (_read_creds().get(ref) or {}).get(field) is not None


def forget_ref(ref: str) -> None:
    data = _read_creds()
    if data.pop(ref, None) is not None:
        _write_creds(data)


# --- device passwords ---------------------------------------------------
def set_password(device_id: str, password: str) -> None:
    set_secret(device_id, "password", password)


def get_password(device_id: str) -> str | None:
    return get_secret(device_id, "password")


def has_password(device_id: str) -> bool:
    return has_secret(device_id, "password")


def forget_device(device_id: str) -> None:
    forget_ref(device_id)


# --- private-key passphrases --------------------------------------------
def set_passphrase(key_ref: str, passphrase: str) -> None:
    """Passphrase for an encrypted private key, stored beside the passwords."""
    set_secret(f"__key__{validate_ref(key_ref)}", "passphrase", passphrase)


def get_passphrase(key_ref: str) -> str | None:
    try:
        slot = f"__key__{validate_ref(key_ref)}"
    except SecretError:
        return None
    return get_secret(slot, "passphrase")
