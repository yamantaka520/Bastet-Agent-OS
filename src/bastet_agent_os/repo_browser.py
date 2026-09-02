"""Bounded read-only browsing of a job's immutable Git evidence commit."""

from __future__ import annotations

import subprocess
from pathlib import PurePosixPath

MAX_TEXT_BYTES = 256 * 1024
MAX_ENTRIES = 1000


class BrowseError(ValueError):
    pass


def clean_path(value: str) -> str:
    if "\x00" in value or "\\" in value:
        raise BrowseError("invalid repository path")
    path = PurePosixPath(value or ".")
    if path.is_absolute() or ".." in path.parts:
        raise BrowseError("repository path must stay below the commit root")
    return "" if str(path) == "." else str(path)


def _git(repo: str, *args: str) -> bytes:
    try:
        proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                              timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrowseError(f"git browse failed: {type(exc).__name__}") from exc
    if proc.returncode:
        # Git diagnostics can contain the private absolute repository path.
        # The browser is viewer-facing, so keep failures deliberately generic.
        raise BrowseError("path not found at evidence commit")
    return proc.stdout


def browse(repo: str, commit: str, requested_path: str = "") -> dict:
    path = clean_path(requested_path)
    _git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
    spec = f"{commit}:{path}" if path else f"{commit}^{{tree}}"
    object_type = _git(repo, "cat-file", "-t", spec).decode().strip()
    if object_type == "tree":
        raw = _git(repo, "ls-tree", "-z", "-l", spec)
        entries = []
        truncated = False
        for record in raw.split(b"\x00"):
            if not record:
                continue
            meta, name = record.split(b"\t", 1)
            mode, kind, sha, size = meta.decode().split(maxsplit=3)
            entries.append({"name": name.decode("utf-8", "replace"),
                            "path": f"{path}/{name.decode('utf-8', 'replace')}".lstrip("/"),
                            "kind": "directory" if kind == "tree" else "file",
                            "mode": mode, "sha": sha,
                            "size": None if size == "-" else int(size)})
            if len(entries) > MAX_ENTRIES:
                entries.pop()
                truncated = True
                break
        return {"kind": "directory", "path": path, "commit": commit,
                "entries": entries, "truncated": truncated}
    if object_type != "blob":
        raise BrowseError("unsupported Git object")
    size = int(_git(repo, "cat-file", "-s", spec).decode())
    if size > MAX_TEXT_BYTES:
        return {"kind": "file", "path": path, "commit": commit,
                "size": size, "binary": False, "truncated": True, "content": ""}
    content = _git(repo, "show", spec)
    if b"\x00" in content:
        return {"kind": "file", "path": path, "commit": commit,
                "size": size, "binary": True, "truncated": False, "content": ""}
    return {"kind": "file", "path": path, "commit": commit, "size": size,
            "binary": False, "truncated": False,
            "content": content.decode("utf-8", "replace")}
