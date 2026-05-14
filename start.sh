#!/usr/bin/env bash
# Start (or restart) all predd daemons via systemd user services.
# Usage: ./start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load JIRA_API_TOKEN from .bashrc if not already set
if [[ -z "${JIRA_API_TOKEN}" ]]; then
  JIRA_API_TOKEN="$(grep -oP '(?<=JIRA_API_TOKEN=)[^\s"'\'']+' ~/.bashrc | tail -1)"
fi

# Write token into hunter systemd override so the service picks it up
if [[ -n "${JIRA_API_TOKEN}" ]]; then
  mkdir -p ~/.config/systemd/user/hunter.service.d
  cat > ~/.config/systemd/user/hunter.service.d/jira.conf <<EOF
[Service]
Environment="JIRA_API_TOKEN=${JIRA_API_TOKEN}"
EOF
  echo "JIRA_API_TOKEN loaded"
else
  echo "WARNING: JIRA_API_TOKEN not found in ~/.bashrc — Jira API ingest will be skipped"
fi

# Reload systemd and restart everything (restarts if running, starts if stopped)
systemctl --user daemon-reload
echo "Restarting all services..."
systemctl --user restart predd hunter obsidian

systemctl --user status predd hunter obsidian --no-pager

echo ""
echo "Monitor logs:"
echo "  tail -f ~/.config/predd/log.txt"
echo "  tail -f ~/.config/predd/hunter-log.txt"
echo "  tail -f ~/.config/predd/obsidian-log.txt"
