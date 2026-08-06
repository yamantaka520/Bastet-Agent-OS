"""Bastet home directory: paths, API token, and runtime configuration."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("BASTET_HOME", str(Path.home() / ".bastet")))
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8890

# where the executor CLIs live; services (systemd/launchd) start with a
# minimal PATH that misses these, breaking both detection and run spawning
TOOL_DIRS = [
    str(Path.home() / ".local/bin"),
    str(Path.home() / ".grok/bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
]


def expand_repo_path(value: str | None) -> str:
    """Turn a stored repo path into a real one.

    A path is typed by a human and stored verbatim, so it arrives as
    `~/Github/thing` or `$HOME/thing`. Neither is a directory: handing that to
    subprocess `cwd` runs the agent somewhere that does not exist — and any tool
    that mkdir's it creates a literal `~` directory, which is how a first
    dispatch ends up executing in an empty non-repo. Expand once, here."""
    if not value:
        return ""
    return str(Path(os.path.expandvars(value.strip())).expanduser())


def is_git_repo(path: str | Path) -> bool:
    """`.git` is a directory in a clone and a file inside a worktree."""
    return (Path(path) / ".git").exists()


def check_repo_path(value: str | None) -> str:
    """Validate a repo path the way the host will use it.

    Absolute only: the path is resolved on the machine running `bastet serve`,
    where a relative path means "wherever the service happened to start".
    Absoluteness is judged by *this* platform, so `C:\\Users\\me\\proj` is
    accepted on Windows and `/home/me/proj` on POSIX — the same string is not
    valid on both, and pretending otherwise moves the failure to dispatch time.
    Returns the expanded path; raises ValueError with what to do instead."""
    expanded = expand_repo_path(value)
    if not expanded:
        raise ValueError("repo 路徑不能空白")
    if not Path(expanded).is_absolute():
        example = (r"C:\Users\you\project" if os.name == "nt"
                   else "/home/you/project")
        raise ValueError(
            f"repo 路徑必須是 Bastet 主機上的絕對路徑（例：{example}）；"
            f"收到的是「{value}」")
    return expanded


def augment_path() -> None:
    """Make sure the well-known tool dirs are on PATH for this process.

    Bastet's own venv bin goes last: gate commands like `pytest -q` need *a*
    runner, and the venv ships one, but a project that provides its own must win.
    """
    import sys

    current = os.environ.get("PATH", "").split(os.pathsep)
    own_bin = str(Path(sys.executable).parent)
    # in our own Docker image the interpreter lives in /usr/local/bin, which is
    # also a TOOL_DIR — prepending it there would put Bastet's pytest ahead of
    # the project's own, the exact opposite of the rule above
    missing = [d for d in TOOL_DIRS
               if d not in current and d != own_bin and Path(d).is_dir()]
    tail = [own_bin] if own_bin not in current and Path(own_bin).is_dir() else []
    if missing or tail:
        os.environ["PATH"] = os.pathsep.join(missing + current + tail)


def gate_tools(db=None) -> list[dict]:
    """Which programs the configured workflows need, and whether they exist.

    The shipped presets run `pytest -q`, `npm test`, `make test`; nothing checked
    that any of them were installed, so a project could reach its test stage and
    fail on a missing runner after spending a whole agent run."""
    import shutil

    from .workflow_presets import PRESETS

    wanted: dict[str, set[str]] = {}
    def note(command: str, source: str) -> None:
        for part in command.replace("&&", ";").replace("||", ";").split(";"):
            program = part.strip().split()[0] if part.strip() else ""
            if program and not program.startswith(("/", ".", "$")):
                wanted.setdefault(program, set()).add(source)

    for preset in PRESETS:
        for stage in preset["stages"]:
            command = (stage.get("gate_config") or {}).get("command")
            if command:
                note(command, f"內建範本 {preset['name']}")
    if db is not None:
        import json as _json
        for row in db.query("SELECT id, stages_json FROM workflow_templates"):
            for stage in _json.loads(row["stages_json"]):
                command = (stage.get("gate_config") or {}).get("command")
                if command:
                    note(command, f"範本 {row['id']}")
    return [{"program": program, "path": shutil.which(program),
             "used_by": sorted(sources)}
            for program, sources in sorted(wanted.items())]


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

    def save_config(self, config: dict) -> None:
        """Rewrite config.json, keeping it 0600 (it names hosts and URLs)."""
        self.config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False))
        os.chmod(self.config_path, 0o600)

    def server_url(self) -> str:
        cfg = self.config()
        return f"http://{cfg.get('host', DEFAULT_HOST)}:{cfg.get('port', DEFAULT_PORT)}"
