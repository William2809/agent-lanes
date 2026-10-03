#!/bin/sh
# ABOUTME: Launches one external worker through the Codex CLI; models, efforts and sandbox limits come from presets (ao-model).
# ABOUTME: Runs detached; writes the transcript to <log> and the process id to <log>.pid.
# Usage: run-worker.sh [--lead] <model> <effort> <sandbox> <repo-dir> <prompt-file> <log-file>
#   model/effort: any model in the presets (`ao-model ls`); effort minimal|low|medium|high|xhigh
#   sandbox:      read-only | workspace-write | full-access, at most what that model's presets allow
#                 full-access = no sandbox (network, SSH, ~/ writes)
#   --lead:       run through the Codex profile in AGENT_LANES_LEAD_PROFILE (e.g. a proxy that lets the
#                 worker spawn other providers' subagents); never with full-access or read-only models.
set -eu
cfg=${AGENT_LANES_CONFIG:-$HOME/.config/agent-lanes/config}
# shellcheck disable=SC1090
[ -f "$cfg" ] && . "$cfg"
lead=0; [ "${1:-}" = "--lead" ] && { lead=1; shift; }
[ $# -eq 6 ] || { sed -n 4,9p "$0"; exit 2; }
model=$1 effort=$2 sandbox=$3 dir=$4 prompt=$5 log=$6
case "$sandbox" in read-only|workspace-write|full-access) ;; *) echo "bad sandbox: $sandbox" >&2; exit 2 ;; esac
preset_route=$(ao-model check "$model" "$effort" "$sandbox") || exit 2
if [ $lead -eq 1 ]; then
  [ -n "${AGENT_LANES_LEAD_PROFILE:-}" ] || { echo "--lead needs AGENT_LANES_LEAD_PROFILE in $cfg" >&2; exit 2; }
  [ "$sandbox" != full-access ] || { echo "full-access is not allowed with --lead (children would inherit it)" >&2; exit 2; }
  ao-model check "$model" "$effort" workspace-write >/dev/null 2>&1 || { echo "--lead is not for read-only models" >&2; exit 2; }
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
# shellcheck disable=SC2086  # $route is intentionally split into flag + value
nohup "$codex" exec $route --model "$model" \
  -c "model_reasoning_effort=\"$effort\"" -s "$sandbox" --skip-git-repo-check \
  -C "$dir" "$(cat "$prompt")" </dev/null >"$log" 2>&1 &
echo $! >"$log.pid"
# Registry read by batches.sh, so a worker that dies is noticed.
mkdir -p "$HOME/.claude/state"
printf '%s\t%s\t%s\t%s\n' "$(date +%s)" "$!" "$log" "$dir" >>"$HOME/.claude/state/workers.tsv"
echo "$log"
