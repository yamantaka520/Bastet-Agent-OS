#!/usr/bin/env bash
# Bastet Agent OS — one-click installer (macOS / Linux)
#
#   curl -fsSL https://raw.githubusercontent.com/yamantaka520/Bastet-Agent-OS/main/install.sh | bash
#
# Installs: Bastet (+ Agent Memory OS latest, claude-agent-sdk) into
# ~/.bastet/venv, plus the executor CLIs via their OFFICIAL installers:
# Claude Code, OpenAI Codex, xAI Grok Build, Google Antigravity (agy),
# NousResearch Hermes, Pi Coding Agent, and OpenClaw. bastet-lite is built in.
#
# Flags / env:
#   --minimal            Bastet + AMOS only, skip executor CLIs
#   --executors "a,b"    only these executors (claude,codex,grok,agy,hermes,pi,openclaw)
#   --upgrade            re-run installers even when a tool already exists
#   --no-service         don't install the boot/login auto-restart service
#   --lan                bind 0.0.0.0 (LAN access; Host guard stays on)
#   BASTET_REPO          override the pip source (default: GitHub main)
set -euo pipefail

# released on PyPI; BASTET_REPO overrides (e.g. git+https://… for main)
REPO="${BASTET_REPO:-bastet-agent-os}"
BASTET_HOME="${BASTET_HOME:-$HOME/.bastet}"
VENV="$BASTET_HOME/venv"
BIN_DIR="$HOME/.local/bin"
MINIMAL=0
UPGRADE=0
SERVICE=1
LAN=0
ONLY_EXECUTORS=""

while [ $# -gt 0 ]; do
  case "$1" in
    --minimal) MINIMAL=1 ;;
    --upgrade) UPGRADE=1 ;;
    --no-service) SERVICE=0 ;;
    --lan) LAN=1 ;;
    --executors) ONLY_EXECUTORS="$2"; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

RESULTS=""
note()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()    { RESULTS="$RESULTS\n  ✓ $*"; printf '  ✓ %s\n' "$*"; }
skip()  { RESULTS="$RESULTS\n  · $*"; printf '  · %s\n' "$*"; }
fail()  { RESULTS="$RESULTS\n  ✗ $*"; printf '  ✗ %s\n' "$*" >&2; }

want() {  # want <name> — is this executor requested?
  [ "$MINIMAL" = 1 ] && return 1
  [ -z "$ONLY_EXECUTORS" ] && return 0
  case ",$ONLY_EXECUTORS," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}

have() { command -v "$1" >/dev/null 2>&1; }

# ---- prerequisites -----------------------------------------------------------

note "檢查前置需求"
PY=""
for cand in python3.12 python3.11 python3; do
  if have "$cand" && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
    PY="$cand"; break
  fi
done
[ -n "$PY" ] || { fail "需要 Python >= 3.11（未找到）"; exit 1; }
have git || { fail "需要 git"; exit 1; }
ok "Python: $($PY --version 2>&1) / git: $(git --version | cut -d' ' -f3)"

# ---- Bastet + Agent Memory OS -------------------------------------------------

note "安裝 Bastet Agent OS + Agent Memory OS（$VENV）"
mkdir -p "$BASTET_HOME" "$BIN_DIR"
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
# pillow: the standard tool for media pipelines (sprite slicing, alpha keying,
# contact sheets). The bastet venv sits last on PATH, which makes a bare
# `python` resolve to it on systems without /usr/bin/python — so it must carry
# what media tasks need, or "No module named PIL" appears only inside runs.
# pytest goes in because the shipped workflow presets run `pytest -q` at their
# test gates; without it a project reaches that stage and fails on a missing
# runner after burning a whole agent run.
if "$VENV/bin/pip" install -q --upgrade "$REPO" 'agent-memory-os[full]' \
     claude-agent-sdk keyring pytest pillow playwright; then
  # Playwright needs a browser, not just the package — without one, the first
  # E2E run dies with "Executable doesn't exist". Chromium only; failures warn
  # instead of aborting the install (air-gapped hosts can add it later).
  "$VENV/bin/playwright" install chromium >/dev/null 2>&1 \
    || echo "  ⚠ playwright chromium download failed — run: $VENV/bin/playwright install chromium"
  ln -sf "$VENV/bin/bastet" "$BIN_DIR/bastet"
  ln -sf "$VENV/bin/agent-memory" "$BIN_DIR/agent-memory" 2>/dev/null || true
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)  # make sure ~/.local/bin is on PATH for future shells
        for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
          [ -f "$rc" ] && ! grep -q '\.local/bin' "$rc" \
            && printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
        done
        skip "已把 ~/.local/bin 加入 PATH（重開 shell 或 source ~/.bashrc 生效）" ;;
  esac
  ok "bastet $("$VENV/bin/bastet" --help >/dev/null 2>&1 && echo ok) + agent-memory-os $("$VENV/bin/pip" show agent-memory-os 2>/dev/null | awk '/^Version/{print $2}')"
else
  fail "Bastet/AMOS pip 安裝失敗"; exit 1
fi
"$VENV/bin/bastet" init >/dev/null && ok "bastet init（~/.bastet）"

# ---- executor CLIs (official installers only) ----------------------------------

install_tool() {  # install_tool <name> <binary> <install command...>
  local name="$1" binary="$2"; shift 2
  want "$name" || { skip "$name（未選擇）"; return 0; }
  if have "$binary" && [ "$UPGRADE" = 0 ]; then
    skip "$name 已安裝（$(command -v "$binary")）"
    return 0
  fi
  note "安裝 $name"
  if "$@"; then ok "$name"; else fail "$name 安裝失敗（可稍後手動安裝）"; fi
}

curl_bash() { curl -fsSL "$1" | bash; }
curl_sh()   { curl -fsSL "$1" | sh; }
openclaw_install() {
  curl -fsSL https://openclaw.ai/install.sh | bash -s -- --no-prompt --no-onboard
}

# 來源皆為各工具官方文件（見 docs/INSTALL 附註）
install_tool claude claude curl_bash https://claude.ai/install.sh
install_tool codex  codex  curl_sh   https://chatgpt.com/codex/install.sh
install_tool grok   grok   curl_bash https://x.ai/cli/install.sh
install_tool agy    agy    curl_bash https://antigravity.google/cli/install.sh
install_tool hermes hermes curl_bash https://hermes-agent.nousresearch.com/install.sh
install_tool pi     pi     curl_sh   https://pi.dev/install.sh
install_tool openclaw openclaw openclaw_install

# ---- LAN mode + service ---------------------------------------------------------

if [ "$LAN" = 1 ]; then
  "$VENV/bin/python" - <<'PYEOF'
import json, os
p = os.path.expanduser(os.environ.get("BASTET_HOME", "~/.bastet")) + "/config.json"
cfg = json.load(open(p)); cfg["host"] = "0.0.0.0"
json.dump(cfg, open(p, "w"), indent=2)
PYEOF
  ok "LAN 模式（bind 0.0.0.0，Host/Origin 防護仍啟用）"
fi

if [ "$SERVICE" = 1 ]; then
  note "安裝開機自啟服務（自動重啟）"
  if "$VENV/bin/bastet" service install; then
    ok "服務常駐（bastet service status 查看）"
  else
    fail "服務安裝失敗 — 可稍後手動執行 bastet service install"
  fi
else
  skip "服務常駐（--no-service）"
fi

# ---- summary -------------------------------------------------------------------

note "健康檢查"
"$VENV/bin/bastet" doctor || true

printf '\n\033[1m安裝結果\033[0m%b\n\n' "$RESULTS"
cat <<'NEXT'
下一步：
  1. 服務已常駐（bastet service status）；未裝服務時手動：bastet serve
  2. Web UI：      http://<本機IP>:8890/ui   （token 在 ~/.bastet/api_token）
  3. 登入各 executor（互動式，需瀏覽器）：
       claude          → 執行 claude 後輸入 /login
       codex login     → ChatGPT OAuth（無頭環境：codex login --device-code）
       grok            → 首次執行自動開瀏覽器
       agy             → 首次執行自動走 Google OAuth
       hermes setup    → 供應商/模型設定精靈
       pi              → 進入後輸入 /login
       openclaw onboard → 帳號與模型設定精靈
     多帳號：在 Web UI「組織」頁建立 executor 帳號，會給你對應的登入指令。
  4. 記憶引擎：     agent-memory doctor（已隨 Bastet 安裝）
NEXT
