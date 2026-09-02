"""Detect numbered sync-conflict copies that shadow build inputs or outputs."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

_COPY = re.compile(r"^(?P<stem>.+) (?P<number>[2-9][0-9]*)(?P<suffix>\.[^.]*)?$")


def conflicts(root: Path) -> list[tuple[Path, Path]]:
    found = []
    for base in (root / "src/bastet_agent_os/ui_dist", root / "web/node_modules/@types"):
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    found = conflicts(Path(args.root).resolve())
    for duplicate, canonical in found:
        print(f"sync-conflict copy: {duplicate} shadows {canonical}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
