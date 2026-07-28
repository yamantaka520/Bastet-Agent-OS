"""Secret references (SPEC §5.8): the DB stores only refs, never values.

Ref schemes:
  keyring:<service>/<name>   OS keyring (macOS Keychain / Windows Credential
                             Manager / Linux Secret Service)
  file:<path>                file contents (0600 expected), for headless setups
  env:<NAME>                 environment variable (dev convenience; discouraged)
"""

from __future__ import annotations

import os
from pathlib import Path


class SecretError(Exception):
    pass


def resolve(ref: str) -> str:
    """Resolve a secret ref to its value. Callers must audit the resolve event."""
    if not ref:
        raise SecretError("empty secret ref")
    scheme, _, rest = ref.partition(":")
    if scheme == "env":
        value = os.environ.get(rest, "")
        if not value:
            raise SecretError(f"env secret {rest!r} is not set")
        return value
    if scheme == "file":
        path = Path(rest).expanduser()
        if not path.exists():
            raise SecretError(f"secret file not found: {mask_path(path)}")
        return path.read_text().strip()
    if scheme == "keyring":
        service, _, name = rest.partition("/")
        try:
            import keyring
        except ImportError as exc:
            raise SecretError("keyring extra not installed (pip install bastet-agent-os[keyring])") from exc
        value = keyring.get_password(service, name)
        if value is None:
            raise SecretError(f"keyring entry {service}/{name} not found")
        return value
    raise SecretError(f"unknown secret ref scheme: {scheme!r}")


def store_keyring(service: str, name: str, value: str) -> str:
    """Store a value in the OS keyring and return its ref."""
    import keyring

    keyring.set_password(service, name, value)
    return f"keyring:{service}/{name}"


def mask(value: str) -> str:
    """Mask a secret for logs/errors: never echo more than a hint."""
    if len(value) <= 8:
        return "***"
    return f"{value[:3]}…{value[-2:]}"


def mask_path(path: Path) -> str:
    return str(path)


FORBIDDEN_CONFIG_KEYS = {"api_key", "apikey", "token", "password", "secret", "authorization"}


def reject_secrets_in_config(config: dict) -> None:
    """Schema guard: config_json must not smuggle secret material (SPEC §5.8)."""
    bad = FORBIDDEN_CONFIG_KEYS.intersection(k.lower() for k in config)
    if bad:
        raise SecretError(
            f"config_json must not contain secret fields {sorted(bad)}; use secret_ref instead"
        )
