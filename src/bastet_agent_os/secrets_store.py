"""Secret references (SPEC §5.8): the DB stores only refs, never values.

Ref schemes:
  keyring:<service>/<name>   OS keyring (macOS Keychain / Windows Credential
                             Manager / Linux Secret Service)
  file:<path>                file contents (0600 expected), for headless setups
  env:<NAME>                 environment variable (dev convenience; discouraged)
"""

from __future__ import annotations

import os
import re
import time
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


_MANAGED_FILE = re.compile(r"^.+-[0-9a-f]{8}$")


def _referenced_files(db) -> set[Path]:
    refs = [row["secret_ref"] for row in db.query(
        "SELECT secret_ref FROM resources WHERE secret_ref IS NOT NULL "
        "UNION ALL SELECT secret_ref FROM channels WHERE secret_ref IS NOT NULL")]
    protected: set[Path] = set()
    for ref in refs:
        concrete = expand(db, ref) if ref.startswith("secret:") else ref
        if concrete.startswith("file:"):
            protected.add(Path(concrete[5:]).expanduser().resolve(strict=False))
    return protected


def prune_managed_files(db, home_root, *, apply: bool = False,
                        minimum_age_s: int = 86400) -> dict:
    """Preview or remove unreferenced files created by :func:`ensure_ref`.

    Only regular, non-symlink, direct children matching Bastet's random-suffix
    naming contract are eligible. A grace period protects a just-rotated file
    from an operator or request race; references are recomputed before removal.
    User-managed ``file:`` paths and arbitrary files in the directory are never
    candidates.
    """
    if minimum_age_s < 0:
        raise SecretError("minimum secret prune age cannot be negative")
    root = (Path(home_root) / "secrets").resolve(strict=False)
    if not root.exists():
        return {"candidates": [], "removed": [], "protected": 0, "bytes": 0}
    current = time.time()
    protected = _referenced_files(db)
    candidates = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file() or not _MANAGED_FILE.fullmatch(path.name):
            continue
        resolved = path.resolve(strict=False)
        if resolved.parent != root or resolved in protected:
            continue
        stat = path.stat()
        age_s = max(0, int(current - stat.st_mtime))
        if age_s < minimum_age_s:
            continue
        candidates.append({"name": path.name, "size": stat.st_size,
                           "age_hours": round(age_s / 3600, 1)})
    removed = []
    if apply:
        # Close the normal rotation/delete-to-prune gap once more immediately
        # before unlink. The grace period is the backstop for concurrent writes.
        protected = _referenced_files(db)
        by_name = {item["name"]: item for item in candidates}
        for name in list(by_name):
            path = root / name
            if path.is_symlink() or not path.is_file() \
                    or path.resolve(strict=False) in protected:
                continue
            path.unlink()
            removed.append(by_name[name])
    return {"candidates": candidates, "removed": removed,
            "protected": len(protected),
            "bytes": sum(item["size"] for item in removed) if apply
                     else sum(item["size"] for item in candidates)}


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


# Patterns that mean "this is credential material, not data". Used to refuse
# putting it somewhere it would travel inside a prompt.
_SECRET_SHAPES = (
    ("PEM 私鑰", re.compile(r"-{5}BEGIN [A-Z0-9 ]*PRIVATE KEY-{5}")),
    ("service account 金鑰", re.compile(r'"private_key"\s*:')),
    ("API key", re.compile(r"\b(sk|rk|pk)-[A-Za-z0-9_-]{16,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{24,}=*")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
)


def smells_like_secret(text: str) -> str:
    """Name the first credential shape found in `text`, or '' if none.

    A heuristic, deliberately one-sided: it exists to stop credentials being
    pasted into places that end up in prompts (job supplies, specs). A false
    positive costs the user a click through the proper credentials card; a false
    negative sends a key to an LLM provider."""
    for label, pattern in _SECRET_SHAPES:
        if pattern.search(text or ""):
            return label
    return ""
