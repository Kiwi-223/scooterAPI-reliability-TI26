#!/usr/bin/env bash
# Run one headless Claude Code task in this repo with a spend cap and a wall-clock limit.
#
#   make agent TASK="Fix the failing test in tests/test_search.py"
#   scripts/agent.sh "Fix the failing test in tests/test_search.py"
#
# Environment (all optional except the task):
#   TASK          the prompt (or pass it as the first argument)
#   BUDGET_USD    spend cap passed to --max-budget-usd          (default: 2)
#   TIMEOUT_MIN   wall-clock limit in minutes                   (default: 20)
#   MAX_TURNS     cap on agentic turns, passed to --max-turns   (default: 40)
#   WORKTREE      run in an isolated git worktree of this name  (default: unset = this checkout)
#   DRY_RUN=1     print the claude command instead of running it (no API call, no spend)
#
# Exit status: claude's own, or 124 if the wall-clock limit stopped it (same as GNU timeout).
#
# Three independent limits: money (--max-budget-usd), turns (--max-turns) and wall-clock time
# (the watchdog below). `--max-turns` is hidden from `claude --help` but is a supported,
# documented print-mode flag (https://code.claude.com/docs/en/cli-reference, and defined in the
# 2.1.310 binary as "Maximum number of agentic turns in non-interactive mode").
set -euo pipefail

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in -h|--help) usage; exit 0 ;; esac

TASK="${1:-${TASK:-}}"
BUDGET_USD="${BUDGET_USD:-2}"
TIMEOUT_MIN="${TIMEOUT_MIN:-20}"
MAX_TURNS="${MAX_TURNS:-40}"
WORKTREE="${WORKTREE:-}"

die() { echo "agent.sh: $*" >&2; exit 2; }
[[ -n "$TASK" ]] || { usage >&2; die "no task given (TASK=\"...\" or first argument)"; }
[[ "$TASK" != -* ]] || die "the task must not start with '-' (it would be read as a claude option)"
[[ "$BUDGET_USD" =~ ^[0-9]+([.][0-9]+)?$ ]] && [[ ! "$BUDGET_USD" =~ ^0+([.]0+)?$ ]] \
  || die "BUDGET_USD must be a positive number, got '$BUDGET_USD'"
[[ "$TIMEOUT_MIN" =~ ^[1-9][0-9]*$ ]] || die "TIMEOUT_MIN must be a positive whole number of minutes, got '$TIMEOUT_MIN'"
[[ "$MAX_TURNS" =~ ^[1-9][0-9]*$ ]] || die "MAX_TURNS must be a positive whole number, got '$MAX_TURNS'"
# A worktree name becomes a directory under .claude/worktrees/: letters, digits, '.', '_', '-',
# starting with a letter or digit, so '.', '..' and anything with '/' are rejected.
[[ -z "$WORKTREE" || ( "$WORKTREE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && "$WORKTREE" != *..* ) ]] \
  || die "WORKTREE must be a simple name (letters, digits, . _ -; no leading '.', no '..'), got '$WORKTREE'"

cmd=(claude -p "$TASK"
  --max-budget-usd "$BUDGET_USD"
  --max-turns "$MAX_TURNS"
  # Edits inside the repo are accepted; any call that would need a human prompt is denied
  # (nobody is watching), so `ask` rules in .claude/settings.json (git push, gh pr create, ...)
  # become hard denials in this mode.
  --permission-mode acceptEdits
  --permission-prompts none
  # Project allow rules are not applied in -p (the workspace is never "trusted" there), so
  # pre-approve the routine commands explicitly. Deny rules from .claude/settings.json still apply.
  --allowed-tools "Bash(make setup)" "Bash(make test)" "Bash(make test *)" \
                  "Bash(git status *)" "Bash(git diff *)" "Bash(git log *)" "Bash(git show *)" \
                  "Bash(git add *)" "Bash(git commit *)"
)
[[ -n "$WORKTREE" ]] && cmd+=(--worktree "$WORKTREE")

if [[ "${DRY_RUN:-}" == "1" ]]; then
  printf 'agent.sh: budget $%s, %s turns, wall clock %s min%s\n' "$BUDGET_USD" "$MAX_TURNS" "$TIMEOUT_MIN" "${WORKTREE:+, worktree $WORKTREE}"
  printf '%q ' "${cmd[@]}"; echo
  exit 0
fi

command -v claude >/dev/null || die "claude CLI not found on PATH"

secs=$(( TIMEOUT_MIN * 60 ))
marker="$(mktemp "${TMPDIR:-/tmp}/agent-timeout.XXXXXX")"; rm -f "$marker"

# Start the agent in its own process group (job control on), so the watchdog can stop it
# together with every command it spawned.
set -m
"${cmd[@]}" </dev/null &
agent_pid=$!
set +m

# Portable watchdog: macOS has no `timeout(1)`. TERM the agent's process group, then KILL it
# 15 s later. Limitation: a process the agent started in a *new* process group or session
# (e.g. via setsid, or a daemonising server) is not in that group and may survive; check with
# `ps` after a timeout.
(
  trap 'kill "$sleeper" 2>/dev/null; exit 0' TERM
  sleep "$secs" & sleeper=$!
  wait "$sleeper"
  : > "$marker"
  echo "agent.sh: wall-clock limit of ${TIMEOUT_MIN} min reached, stopping the agent" >&2
  kill -TERM -- "-$agent_pid" 2>/dev/null || exit 0
  sleep 15 & sleeper=$!; wait "$sleeper"
  kill -KILL -- "-$agent_pid" 2>/dev/null || true
) &
watchdog_pid=$!

cleanup() { kill "$watchdog_pid" 2>/dev/null || true; kill -TERM -- "-$agent_pid" 2>/dev/null || true; }
trap 'cleanup; exit 130' INT TERM

set +e
wait "$agent_pid"; status=$?
set -e
kill "$watchdog_pid" 2>/dev/null || true
wait "$watchdog_pid" 2>/dev/null || true

if [[ -e "$marker" ]]; then rm -f "$marker"; exit 124; fi
exit "$status"
