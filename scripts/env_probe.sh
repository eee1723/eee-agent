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

printf 'agent venv:\n'
VENV_PY=""
if [ -x ".venv/Scripts/python.exe" ]; then
  VENV_PY=".venv/Scripts/python.exe"
  ok ".venv present ($("$VENV_PY" -B -c \
    "import sys; sys.stdout.write('Python ' + sys.version.split()[0])" 2>&1))"
else
  warn ".venv missing; run uv sync --extra eval"
fi

printf 'config / keys:\n'
ENV_FILE="${EEE_PROBE_ENV_FILE:-.env}"
if [ -f "$ENV_FILE" ]; then
  [ "$ENV_FILE" = ".env" ] && ok ".env present" || ok "environment file present"
  DOTENV_PY="$VENV_PY"
  if [ -z "$DOTENV_PY" ] && [ -n "$HOU" ] && [ -x "$HOU/python311/python.exe" ]; then
    DOTENV_PY="$HOU/python311/python.exe"
  fi
  if [ -n "$DOTENV_PY" ]; then
    KEY_STATUS=$("$DOTENV_PY" -B -c \
      "import sys; from dotenv import dotenv_values; value = dotenv_values(sys.argv[1]).get('DEEPSEEK_API_KEY'); status = 'missing' if value is None else 'placeholder' if value in ('', 'sk-your-deepseek-key') else 'set'; sys.stdout.write(status)" \
      "$ENV_FILE" 2>/dev/null)
  else
    KEY_STATUS=""
  fi
  case "$KEY_STATUS" in
    missing) warn "DEEPSEEK_API_KEY is missing" ;;
    placeholder) warn "DEEPSEEK_API_KEY is empty or a placeholder" ;;
    set) ok "DEEPSEEK_API_KEY is set" ;;
    *) warn "could not inspect DEEPSEEK_API_KEY with python-dotenv" ;;
  esac
else
  [ "$ENV_FILE" = ".env" ] && warn ".env missing; create it from .env.example" || \
    warn "environment file missing: $ENV_FILE"
fi

printf 'Secure Runtime / Bridge:\n'
na "run the authenticated Runtime Control smoke when Houdini integration is required"

printf 'agent harness:\n'
if [ -n "$VENV_PY" ]; then
  if "$VENV_PY" -B -c \
    "import os; os.environ['EEE_CONTEXTSEEK'] = 'false'; os.environ['EEE_TRACING'] = ''; os.environ.setdefault('DEEPSEEK_API_KEY', 'probe-placeholder-not-used'); from eee_agent.app import build_agent; from eee_agent.runtime.agent_tools import build_read_only_tools; build_agent(tools=build_read_only_tools())" \
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
