"""Bastet home directory: paths, API token, and runtime configuration."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("BASTET_HOME", str(Path.home() / ".bastet")))
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8890


class Home:
    """Filesystem layout under ~/.bastet (override with BASTET_HOME)."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else DEFAULT_HOME

    @property
    def db_path(self) -> Path:
        return self.root / "bastet.db"

    @property
    def token_path(self) -> Path:
        return self.root / "api_token"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def worktrees_dir(self) -> Path:
        return self.root / "worktrees"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.worktrees_dir.mkdir(exist_ok=True)
        self.artifacts_dir.mkdir(exist_ok=True)
        if not self.token_path.exists():
            self.token_path.write_text(secrets.token_urlsafe(32))
            os.chmod(self.token_path, 0o600)
        if not self.config_path.exists():
            self.config_path.write_text(
                json.dumps({"host": DEFAULT_HOST, "port": DEFAULT_PORT}, indent=2)
            )
            os.chmod(self.config_path, 0o600)

    def api_token(self) -> str:
        return self.token_path.read_text().strip()

    def config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"host": DEFAULT_HOST, "port": DEFAULT_PORT}

    def server_url(self) -> str:
        cfg = self.config()
        return f"http://{cfg.get('host', DEFAULT_HOST)}:{cfg.get('port', DEFAULT_PORT)}"
