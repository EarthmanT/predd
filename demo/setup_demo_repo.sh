#!/usr/bin/env bash
# Setup a throwaway GitHub repo for the predd demo.
# Creates {GITHUB_USER}/predd-demo, pushes dummy codebase, prints config snippet.
#
# Usage:
#   bash demo/setup_demo_repo.sh
#
# Requires: gh CLI (gh auth login), git

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEBASE_DIR="$SCRIPT_DIR/dummy_codebase"

# Detect GitHub user
GITHUB_USER="${GITHUB_USER:-$(gh api user --jq '.login' 2>/dev/null)}"
if [[ -z "$GITHUB_USER" ]]; then
  echo "ERROR: Could not detect GitHub user. Set GITHUB_USER env var or run 'gh auth login'."
  exit 1
fi

REPO_NAME="predd-demo"
FULL_REPO="$GITHUB_USER/$REPO_NAME"

echo "=== predd demo setup ==="
echo "GitHub user : $GITHUB_USER"
echo "Repo        : $FULL_REPO"
echo ""

# Create the repo (skip if already exists)
if gh repo view "$FULL_REPO" &>/dev/null; then
  echo "Repo $FULL_REPO already exists — skipping creation."
else
  echo "Creating $FULL_REPO..."
  gh repo create "$FULL_REPO" --public --description "predd demo repo" --confirm 2>/dev/null || \
    gh repo create "$FULL_REPO" --public --description "predd demo repo"
fi

# Clone into temp dir and push dummy codebase
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

echo "Cloning into temp dir..."
gh repo clone "$FULL_REPO" "$TMPDIR/repo" -- --quiet

echo "Copying dummy codebase..."
cp -r "$CODEBASE_DIR/." "$TMPDIR/repo/"

cd "$TMPDIR/repo"
git add -A
git diff --cached --quiet && echo "Nothing to push — repo already up to date." || {
  git commit -m "chore: initial demo codebase"
  git push
  echo "Pushed dummy codebase."
}

# Create labels that hunter expects for repo routing
echo "Creating repo label for Jira routing..."
gh label create "predd-demo" --repo "$FULL_REPO" --color "0075ca" --description "predd demo issues" 2>/dev/null || true

echo ""
echo "================================================================"
echo "Setup complete! Add this to ~/.config/predd/config.toml:"
echo "================================================================"
echo ""
cat <<EOF
# --- Demo repo ---
[[repo]]
name = "$FULL_REPO"
predd = true
hunter = true
obsidian = false

# Point Jira at the mock server (run: python demo/mock_jira.py)
jira_base_url = "http://localhost:8081"
jira_api_enabled = true
jira_projects = ["DEMO"]
jira_sprint_filter = "active"
EOF
echo ""
echo "Then start the mock Jira server:"
echo "  python demo/mock_jira.py &"
echo ""
echo "And restart predd:"
echo "  ./start.sh"
echo ""
echo "Watch the action:"
echo "  tail -f ~/.config/predd/hunter-log.txt"
