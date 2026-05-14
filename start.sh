#!/usr/bin/env bash
# Start all predd daemons in tmux sessions.
# Usage: ./start.sh [--restart]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

restart=false
if [[ "${1}" == "--restart" ]]; then
  restart=true
fi

start_session() {
  local name=$1
  local cmd=$2

  if tmux has-session -t "$name" 2>/dev/null; then
    if $restart; then
      echo "Stopping $name..."
      tmux kill-session -t "$name"
    else
      echo "$name already running — skipping (use --restart to force)"
      return
    fi
  fi

  echo "Starting $name..."
  tmux new-session -d -s "$name" -c "$SCRIPT_DIR" "$cmd"
  echo "  $name started (tmux attach -t $name)"
}

start_session predd   "uv run predd.py start"
start_session hunter  "uv run hunter.py start"
start_session obsidian "uv run obsidian.py start"

echo ""
echo "All services started. Monitor logs:"
echo "  tail -f ~/.config/predd/log.txt"
echo "  tail -f ~/.config/predd/hunter-log.txt"
echo "  tail -f ~/.config/predd/obsidian-log.txt"
