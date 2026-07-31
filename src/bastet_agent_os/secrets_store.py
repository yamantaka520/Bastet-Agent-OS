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


def expand(db, ref: str) -> str:
    """Resolve the `secret:<resource_id>` indirection to a concrete ref.

    Resources point at saved credentials instead of copying their ref, so
    rotating a credential in one place changes every resource that uses it.
    Concrete refs (keyring:/file:/env:) pass straight through."""
    if not ref.startswith("secret:"):
        return ref
    secret_id = ref.split(":", 1)[1]
    row = db.one("SELECT secret_ref, name FROM resources WHERE id=? AND kind='secret'",
                 (secret_id,))
    if row is None:
        raise SecretError(f"credential {secret_id} not found in the pool")
    if not row["secret_ref"]:
        raise SecretError(f"credential {row['name']} has no ref")
    return row["secret_ref"]


def store_keyring(service: str, name: str, value: str) -> str:
    """Store a value in the OS keyring and return its ref."""
    import keyring

    keyring.set_password(service, name, value)
    return f"keyring:{service}/{name}"


KNOWN_SCHEMES = ("env:", "file:", "keyring:", "secret:")


PEM_MARKERS = ("PRIVATE KEY", "CERTIFICATE")


def normalise_private_key(value: str) -> tuple[str, bool]:
    """Restore the line structure a single-line input destroyed.

    A PEM key pasted into a one-line field arrives as
    `-----BEGIN OPENSSH PRIVATE KEY----- AAAA…== -----END …-----` and ssh answers
    `error in libcrypto`. Header and footer make the intended structure
    unambiguous, so re-wrapping is a repair, not a guess. Returns
    (value, repaired)."""
    text = (value or "").strip()
    if "\n" in text or not any(marker in text for marker in PEM_MARKERS):
        return value, False
    import re

    match = re.match(r"^(-{5}BEGIN [A-Z0-9 ]+-{5})(.*?)(-{5}END [A-Z0-9 ]+-{5})$",
                     text, re.S)
    if not match:
        return value, False
    header, body, footer = match.groups()
    body = "".join(body.split())          # the base64 payload, whitespace removed
    if not body:
        return value, False
    wrapped = "\n".join(body[i:i + 70] for i in range(0, len(body), 70))
    return f"{header}\n{wrapped}\n{footer}\n", True


def ensure_ref(value: str, home_root, hint: str) -> str:
    """Accept either a proper secret ref or a RAW secret value.

    People paste tokens straight into ref fields; failing silently at
    channel/resource startup is worse than securing the value for them:
    raw input lands in <home>/secrets/<hint> (0600) and a file: ref is
    returned. Real refs pass through untouched."""
    import secrets as _secrets
    from pathlib import Path

    value = (value or "").strip()
    if not value or value.startswith(KNOWN_SCHEMES):
        return value
    value, _ = normalise_private_key(value)   # a one-line paste is still a key
    secrets_dir = Path(home_root) / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets_dir, 0o700)
    path = secrets_dir / f"{hint}-{_secrets.token_hex(4)}"
    path.write_text(value)
    os.chmod(path, 0o600)
    return f"file:{path}"


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
