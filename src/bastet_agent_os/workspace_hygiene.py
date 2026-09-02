"""Detect numbered sync-conflict copies that shadow build inputs or outputs."""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

_COPY = re.compile(r"^(?P<stem>.+) (?P<number>[2-9][0-9]*)(?P<suffix>\.[^.]*)?$")
_GENERATED_ROOTS = ("src/bastet_agent_os/ui_dist", "web/node_modules/@types")


def conflicts(root: Path) -> list[tuple[Path, Path]]:
    found = []
    for relative in _GENERATED_ROOTS:
        base = root / relative
        if not base.exists():
            continue
        for path in base.rglob("*"):
            match = _COPY.match(path.name)
            if not match:
                continue
            canonical = path.with_name(match.group("stem") + (match.group("suffix") or ""))
            if canonical.exists():
                found.append((path, canonical))
    return sorted(found)


def clean_conflicts(root: Path) -> list[Path]:
    """Remove only recognized numbered copies below reproducible directories.

    The canonical sibling must exist (enforced by :func:`conflicts`).  Sorting
    deepest-first makes duplicate directories safe even if they contain another
    numbered copy.  Symlinks are unlinked, never followed.
    """
    removed = []
    candidates = [duplicate for duplicate, _canonical in conflicts(root)]
    for duplicate in sorted(candidates, key=lambda path: len(path.parts), reverse=True):
        if not duplicate.exists() and not duplicate.is_symlink():
            continue
        if duplicate.is_dir() and not duplicate.is_symlink():
            shutil.rmtree(duplicate)
        else:
            duplicate.unlink()
        removed.append(duplicate)
    return sorted(removed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument(
        "--clean", action="store_true",
        help="remove recognized copies only from reproducible Web directories")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if args.clean:
        removed = clean_conflicts(root)
        for duplicate in removed:
            print(f"removed sync-conflict copy: {duplicate}")
        return 0
    found = conflicts(root)
    for duplicate, canonical in found:
        print(f"sync-conflict copy: {duplicate} shadows {canonical}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
