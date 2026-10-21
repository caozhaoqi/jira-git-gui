"""Small adapter for the operating system's credential store."""
from __future__ import annotations

import uuid
from urllib.parse import quote, unquote

_SERVICE = "Jira Git GUI"
_REF_PREFIX = "keyring://jira-git-gui/"


class SecureStorageError(RuntimeError):
    """Credential storage is unavailable or a referenced secret is missing."""


def _keyring():
    try:
        import keyring
    except ImportError as exc:
        raise SecureStorageError(
            "Secure credential storage is unavailable; install the keyring dependency."
        ) from exc

    from keyring.backends.fail import Keyring as FailKeyring

    backend = keyring.get_keyring()
    if isinstance(backend, FailKeyring):
        raise SecureStorageError("No operating-system credential store is available.")
    return keyring


def secret_ref(name: str) -> str:
    return _REF_PREFIX + quote(name, safe="")


def store_secret(name: str, value: str) -> str:
    if not value:
        return ""
    versioned_name = f"{name}:{uuid.uuid4().hex}"
    try:
        _keyring().set_password(_SERVICE, versioned_name, value)
    except SecureStorageError:
        raise
    except Exception as exc:
        raise SecureStorageError("Could not save a secret to the operating-system credential store.") from exc
    return secret_ref(versioned_name)


def load_secret(ref: str) -> str:
    if not ref.startswith(_REF_PREFIX):
        raise SecureStorageError("Invalid secure credential reference.")
    name = unquote(ref[len(_REF_PREFIX):])
    if not name:
        raise SecureStorageError("Invalid secure credential reference.")
    try:
        value = _keyring().get_password(_SERVICE, name)
    except SecureStorageError:
        raise
    except Exception as exc:
        raise SecureStorageError("Could not read a secret from the operating-system credential store.") from exc
    if value is None:
        raise SecureStorageError("A referenced secret is missing from the operating-system credential store.")
    return value


def delete_secret(ref: str) -> None:
    if not ref.startswith(_REF_PREFIX):
        return
    name = unquote(ref[len(_REF_PREFIX):])
    try:
        _keyring().delete_password(_SERVICE, name)
    except Exception as exc:
        raise SecureStorageError("Could not delete a secret from the operating-system credential store.") from exc
