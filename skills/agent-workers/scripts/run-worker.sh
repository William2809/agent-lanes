#!/bin/sh
# ABOUTME: Launches one external worker through the Codex CLI or another harness (WK_HARNESS: cursor, opencode, pi, omp;
# ABOUTME: harness_run.py writes their logs in the Codex shape); models, efforts and sandbox limits come from presets (ao-model).
# ABOUTME: Runs detached; records metadata, final message, exit status and the real worker PID next to <log>.
# Usage: run-worker.sh [--lead] <model> <effort> <sandbox> <repo-dir> <prompt-file> <log-file>
#   model/effort: any model in the presets (`ao-model ls`); effort minimal|low|medium|high|xhigh
#   sandbox:      read-only | workspace-write | full-access, at most what that model's presets allow
#                 full-access = no sandbox (network, SSH, ~/ writes)
#   --lead:       run through the Codex profile in AGENT_LANES_LEAD_PROFILE (e.g. a proxy that lets the
#                 worker spawn other providers' subagents); never with full-access or read-only models.
set -eu
tool_root=$(cd "$(dirname "$(readlink -f "$0")")/../../.." && pwd)
tool_rev=$(git -C "$tool_root" rev-parse HEAD 2>/dev/null || echo unknown)
_run_scripts="$tool_root/skills/agent-workers/scripts"
. "$_run_scripts/state.sh"
. "$_run_scripts/run-record.sh"
cfg=${AGENT_LANES_CONFIG:-$HOME/.config/agent-lanes/config}
# shellcheck disable=SC1090
[ -f "$cfg" ] && . "$cfg"
lead=0; [ "${1:-}" = "--lead" ] && { lead=1; shift; }
[ $# -eq 6 ] || { sed -n 4,9p "$0"; exit 2; }
model=$1 effort=$2 sandbox=$3 dir=$4 prompt=$5 log=$6
case "$sandbox" in read-only|workspace-write|full-access) ;; *) echo "bad sandbox: $sandbox" >&2; exit 2 ;; esac
harness=${WK_HARNESS:-codex}
preset_route=$("$tool_root/bin/ao-model" check "$model" "$effort" "$sandbox" "$harness") || exit 2
# wk passes the chosen preset's own route; re-deriving it from model/effort/sandbox can pick another preset's.
[ -z "${WK_ROUTE:-}" ] || preset_route=$WK_ROUTE
if [ "$harness" != codex ]; then
  [ $lead -eq 0 ] || { echo "--lead is Codex-only" >&2; exit 2; }
  python3 "$tool_root/skills/agent-workers/scripts/harness_run.py" check "$harness" "$sandbox" || exit 2
  [ -d "$dir" ] || { echo "repo dir not found: $dir" >&2; exit 1; }
  [ -s "$prompt" ] || { echo "prompt file empty or missing: $prompt" >&2; exit 1; }
  mkdir -p "$(dirname "$log")"
  ops_run_record "$log" "${WK_PRESET:-}" "$harness" "$preset_route" "$model" "$effort" "$sandbox" "$dir" ""
  WK_ROUTE=$preset_route ops_launch "$log" "$dir" "$sandbox" "${WK_PRESET:-}" python3 "$_run_scripts/harness_run.py" run "$harness" "$model" "$effort" "$sandbox" "$dir" "$prompt"
  echo "$log"; exit 0
fi
if [ $lead -eq 1 ]; then
  [ -n "${AGENT_LANES_LEAD_PROFILE:-}" ] || { echo "--lead needs AGENT_LANES_LEAD_PROFILE in $cfg" >&2; exit 2; }
  [ "$sandbox" != full-access ] || { echo "full-access is not allowed with --lead (children would inherit it)" >&2; exit 2; }
  "$tool_root/bin/ao-model" check "$model" "$effort" workspace-write codex >/dev/null 2>&1 || { echo "--lead is not for read-only models" >&2; exit 2; }
  route="--profile $AGENT_LANES_LEAD_PROFILE"
elif [ "$preset_route" = codex ]; then route=""
else route="--profile $preset_route"; fi
[ "$sandbox" = full-access ] && sandbox=danger-full-access
codex=${CODEX_BIN:-$(command -v codex || true)}
[ -x "$codex" ] || codex=/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex
[ -x "$codex" ] || { echo "codex CLI not found" >&2; exit 1; }
[ -d "$dir" ] || { echo "repo dir not found: $dir" >&2; exit 1; }
[ -s "$prompt" ] || { echo "prompt file empty or missing: $prompt" >&2; exit 1; }
mkdir -p "$(dirname "$log")"
record_route=$preset_route; [ $lead -eq 0 ] || record_route=$AGENT_LANES_LEAD_PROFILE
ops_run_record "$log" "${WK_PRESET:-}" codex "$record_route" "$model" "$effort" "$sandbox" "$dir" ""
# shellcheck disable=SC2086  # $route is intentionally split into flag + value
ops_launch "$log" "$dir" "$sandbox" "${WK_PRESET:-}" "$codex" exec $route --color never -o "$log.last" --model "$model" \
  -c "model_reasoning_effort=\"$effort\"" -s "$sandbox" --skip-git-repo-check \
  -C "$dir" "$(cat "$prompt")"
echo "$log"
