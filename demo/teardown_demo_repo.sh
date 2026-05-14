#!/usr/bin/env bash
# Delete the predd demo repo from GitHub.
#
# Usage:
#   bash demo/teardown_demo_repo.sh

set -e

GITHUB_USER="${GITHUB_USER:-$(gh api user --jq '.login' 2>/dev/null)}"
FULL_REPO="$GITHUB_USER/predd-demo"

echo "This will DELETE $FULL_REPO from GitHub."
read -r -p "Are you sure? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

gh repo delete "$FULL_REPO" --yes
echo "Deleted $FULL_REPO."

pkill -f mock_jira.py 2>/dev/null && echo "Stopped mock Jira." || true

echo ""
echo "Remember to remove the [[repo]] block for $FULL_REPO from ~/.config/predd/config.toml"
echo "and restore jira_base_url / jira_projects, then ./start.sh"
