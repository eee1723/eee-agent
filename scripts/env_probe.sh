#!/usr/bin/env bash
# EEE Agent environment probe. It is read-only and never fails the session.
set +e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT" 2>/dev/null

ok()   { printf '  [ok]   %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
na()   { printf '  [ -- ] %s\n' "$*"; }

printf '=== EEE Agent environment probe | host=%s | %s ===\n' \
  "$(hostname 2>/dev/null)" "$(date '+%F %T' 2>/dev/null)"
printf 'repo: %s\n' "$ROOT"

printf 'Houdini 21 install:\n'
HOU=""
for cand in \
  "${HOUDINI_ROOT:-}" \
  "/mnt/d/houdini" \
  "/d/houdini" \
  "/c/Program Files/Side Effects Software/Houdini 21.0.440"
do
  if [ -n "$cand" ] && [ -f "$cand/bin/hython.exe" ]; then
    ok "found at $cand"
    HOU="$cand"
    break
  fi
done
if [ -z "$HOU" ]; then
  warn "not at known paths; set HOUDINI_ROOT when Houdini is installed elsewhere"
fi

HOU_RPYC=""
if [ -n "$HOU" ] && [ -x "$HOU/python311/python.exe" ]; then
  HOU_RPYC=$("$HOU/python311/python.exe" -c \
    "import rpyc; print('.'.join(map(str, rpyc.version.version)))" 2>/dev/null)
  [ -n "$HOU_RPYC" ] && ok "Houdini bundled rpyc = $HOU_RPYC" || \
    na "could not read Houdini rpyc version"
fi

printf 'agent venv:\n'
VENV_PY=""
VENV_RPYC=""
if [ -x ".venv/Scripts/python.exe" ]; then
  VENV_PY=".venv/Scripts/python.exe"
  ok ".venv present ($("$VENV_PY" --version 2>&1))"
  VENV_RPYC=$("$VENV_PY" -c \
    "import rpyc; print('.'.join(map(str, rpyc.version.version)))" 2>/dev/null)
  if [ -n "$VENV_RPYC" ]; then
    ok "venv rpyc = $VENV_RPYC"
    if [ -n "$HOU_RPYC" ] && [ "$HOU_RPYC" != "$VENV_RPYC" ]; then
      warn "rpyc mismatch: venv $VENV_RPYC vs Houdini $HOU_RPYC"
    fi
  else
    warn ".venv exists but rpyc is missing; run uv sync --extra eval"
  fi
else
  warn ".venv missing; run uv sync --extra eval"
fi

printf 'config / keys:\n'
if [ -f ".env" ]; then
  ok ".env present"
  KEY=$(grep -E '^DEEPSEEK_API_KEY=' .env 2>/dev/null | head -1 | cut -d= -f2-)
  case "$KEY" in
    ""|"sk-your-deepseek-key") warn "DEEPSEEK_API_KEY is empty or a placeholder" ;;
    *) ok "DEEPSEEK_API_KEY is set" ;;
  esac
else
  warn ".env missing; create it from .env.example"
fi

PORTPY="$VENV_PY"
if [ -z "$PORTPY" ] && [ -n "$HOU" ] && [ -x "$HOU/python311/python.exe" ]; then
  PORTPY="$HOU/python311/python.exe"
fi

printf 'Houdini RPC bridge (127.0.0.1:18811):\n'
if [ -n "$PORTPY" ]; then
  if "$PORTPY" -c "import socket
s = socket.socket()
s.settimeout(1.0)
try:
    s.connect(('127.0.0.1', 18811))
except Exception:
    raise SystemExit(1)
finally:
    s.close()" 2>/dev/null; then
    ok "UP"
  else
    na "not listening; only required for Houdini integration runs"
  fi
else
  na "no Python available to test the port"
fi

printf 'agent harness:\n'
if [ -n "$VENV_PY" ]; then
  if "$VENV_PY" -c \
    "import os; os.environ.setdefault('DEEPSEEK_API_KEY', 'probe-placeholder-not-used'); from eee_agent.app import build_agent; build_agent()" \
    >/dev/null 2>&1; then
    ok "build_agent() compiles"
  else
    warn "build_agent() failed; run uv sync --extra eval and inspect the error"
  fi
else
  na "skip harness check because the venv is unavailable"
fi

printf '=== end probe ===\n'
exit 0
