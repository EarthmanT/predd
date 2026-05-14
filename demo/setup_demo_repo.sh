#!/usr/bin/env bash
# Create a throwaway GitHub repo for the predd demo.
# Pushes a small intentionally-incomplete Python API so the demo issues have real code to work on.
#
# Usage:
#   bash demo/setup_demo_repo.sh

set -e

GITHUB_USER="${GITHUB_USER:-$(gh api user --jq '.login' 2>/dev/null)}"
if [[ -z "$GITHUB_USER" ]]; then
  echo "ERROR: Could not detect GitHub user. Run 'gh auth login'."
  exit 1
fi

FULL_REPO="$GITHUB_USER/predd-demo"

echo "=== predd demo setup ==="
echo "Repo: $FULL_REPO"
echo ""

# Create repo
if gh repo view "$FULL_REPO" &>/dev/null; then
  echo "Repo already exists — skipping creation."
else
  gh repo create "$FULL_REPO" --public --description "predd demo repo" --confirm 2>/dev/null || \
    gh repo create "$FULL_REPO" --public --description "predd demo repo"
  echo "Created $FULL_REPO."
fi

# Push starter codebase
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT
gh repo clone "$FULL_REPO" "$TMPDIR/repo" -- --quiet

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp -r "$SCRIPT_DIR/dummy_codebase/." "$TMPDIR/repo/"

cd "$TMPDIR/repo"
git add -A
git diff --cached --quiet && echo "Codebase already pushed." || {
  git commit -m "chore: initial demo codebase"
  git push
  echo "Pushed starter codebase."
}

echo ""
echo "================================================================"
echo "Done! Now:"
echo "================================================================"
echo ""
echo "1. Add this block to ~/.config/predd/config.toml:"
echo ""
cat <<EOF
[[repo]]
name = "$FULL_REPO"
predd = true
hunter = true
obsidian = false
EOF
echo ""
echo "2. Start mock Jira (in a separate terminal):"
echo "   python demo/mock_jira.py --repo $FULL_REPO"
echo ""
echo "3. Update jira_base_url and jira_projects in config.toml:"
echo "   jira_base_url = \"http://localhost:8081\""
echo "   jira_projects = [\"DEMO\"]"
echo ""
echo "4. Restart: ./start.sh"
echo "5. Watch:   tail -f ~/.config/predd/hunter-log.txt"
