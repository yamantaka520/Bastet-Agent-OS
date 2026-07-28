"""Container isolation for runs (SPEC §5.4.3, D6).

Wraps an executor's command in `docker run` with the mount and network rules
the security review demanded:

- workdir mounted read-write, but NEVER the main repo's `.git` writable — a
  git worktree's `.git` file points into the main repo, and a writable mount
  lets the agent plant `.git/hooks` / `core.fsmonitor` payloads that execute
  on the host later. The orchestrator therefore mounts the worktree and the
  main `.git` READ-ONLY; in-container git can read history but not write it
  (commits happen on the host after diff collection).
- non-root user, no docker socket, CPU/memory limits.
- the gateway is reached via host.docker.internal (host-gateway alias). Note
  Linux: services bound to 127.0.0.1 are unreachable from containers; the
  gateway must also listen on the docker bridge there (documented limitation
  until the gateway grows a --bind-docker flag).

No Docker on the host => runs requiring containers fail loudly (queue/fail,
never a silent downgrade to worktree).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

DEFAULT_IMAGE = "python:3.12-slim"
DEFAULT_CPUS = "2"
DEFAULT_MEMORY = "2g"
DEFAULT_USER = "1000:1000"


class ContainerUnavailable(Exception):
    pass


@dataclass
class ContainerSpec:
    workdir: str                      # host path mounted at /work
    image: str = DEFAULT_IMAGE
    env: dict[str, str] | None = None
    git_common_dir: str | None = None  # main repo .git, mounted read-only
    cpus: str = DEFAULT_CPUS
    memory: str = DEFAULT_MEMORY
    user: str = DEFAULT_USER


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=15)
    return probe.returncode == 0


def rewrite_gateway_url(url: str) -> str:
    """127.0.0.1/localhost is the container itself; route to the host instead."""
    return (url.replace("127.0.0.1", "host.docker.internal")
               .replace("localhost", "host.docker.internal"))


def wrap_command(command: list[str], spec: ContainerSpec) -> list[str]:
    """Wrap `command` in docker run with the SPEC §5.4.3 mount/network rules."""
    args = [
        "docker", "run", "--rm",
        "--user", spec.user,
        "--cpus", spec.cpus,
        "--memory", spec.memory,
        "--security-opt", "no-new-privileges",
        "--add-host", "host.docker.internal:host-gateway",
        "-v", f"{spec.workdir}:/work",
        "-w", "/work",
    ]
    if spec.git_common_dir:
        args += ["-v", f"{spec.git_common_dir}:{spec.git_common_dir}:ro"]
    for key, value in (spec.env or {}).items():
        args += ["-e", f"{key}={value}"]
    args.append(spec.image)
    args += command
    return args


def ensure_available() -> None:
    if not docker_available():
        raise ContainerUnavailable(
            "isolation=container requested but Docker is not available — "
            "the run fails rather than silently downgrading (SPEC §5.4.3)")
