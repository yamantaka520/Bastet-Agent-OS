# Bastet Agent OS — control plane image.
#
#   docker run -d -p 8890:8890 -v bastet-home:/data yamantaka520/bastet-agent-os
#
# What this image is: the control plane, the gateway, the WebUI, and the
# bastet-lite executor. What it deliberately is NOT: the vendor CLIs
# (claude/codex/grok/agy/hermes/pi/openclaw) — their logins are interactive and their
# credentials are yours, so they belong on a host (or your own derived image),
# not baked into a public one. Point repos and executors at the container via
# mounts, or run Bastet on the host for full executor support and keep this
# image for gateway/board/memory duty.
FROM python:3.12-slim

# git: every run happens in a worktree of the project's repo
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# the whole state lives in one directory; mount it to keep it
ENV BASTET_HOME=/data \
    AGENT_MEMORY_HOME=/data/agent-memory \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . 'agent-memory-os[full]' pytest pillow playwright \
 && playwright install --with-deps chromium

# non-root: the container has no business as uid 0, and worktrees/secrets are
# all under /data anyway. /data is created and chowned BEFORE the VOLUME line —
# an anonymous volume copies the image directory's ownership, and a root-owned
# /data makes `bastet init` die on the first mkdir.
RUN useradd -m -u 1000 bastet && mkdir -p /data && chown -R bastet:bastet /data
VOLUME /data
USER bastet

EXPOSE 8890
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD curl -fs http://127.0.0.1:8890/api/version || exit 1

# init is idempotent; 0.0.0.0 because the container boundary replaces the
# loopback default (the Host allow-list stays on)
CMD ["/bin/sh", "-c", "bastet init >/dev/null 2>&1 || true; exec bastet serve --host 0.0.0.0 --port 8890"]
