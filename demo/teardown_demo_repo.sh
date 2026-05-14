#!/usr/bin/env bash
# Tear down the predd demo repo and clean up.
#
# Usage:
#   bash demo/teardown_demo_repo.sh

set -e

GITHUB_USER="${GITHUB_USER:-$(gh api user --jq '.login' 2>/dev/null)}"
FULL_REPO="$GITHUB_USER/predd-demo"

echo "=== predd demo teardown ==="
echo "This will DELETE $FULL_REPO from GitHub."
read -r -p "Are you sure? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

gh repo delete "$FULL_REPO" --yes
echo "Deleted $FULL_REPO."

echo ""
echo "Remember to:"
echo "  1. Remove the [[repo]] block for $FULL_REPO from ~/.config/predd/config.toml"
echo "  2. Remove any state entries: grep -v 'predd-demo' ~/.config/predd/hunter-state.json"
echo "  3. Stop mock Jira if still running: pkill -f mock_jira.py"
