#!/usr/bin/env bash
# Personal worktree management script
# Usage: ~/.config/devin/scripts/worktree.sh <command> [args...]

set -euo pipefail

# Detect repo from current directory
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [[ -z "$REPO_ROOT" ]]; then
    echo "Error: Not in a git repository" >&2
    exit 1
fi

REPO_NAME="$(basename "$REPO_ROOT")"
WORKTREE_BASE="${HOME}/windsurf/worktrees/${REPO_NAME}"

usage() {
    cat << 'USAGE'
Personal Worktree Management Script

Usage:
  worktree.sh create <branch-name> [base-branch]
  worktree.sh checkout <branch-name>
  worktree.sh list
  worktree.sh remove <branch-name>
  worktree.sh pr <pr-number>

Commands:
  create <branch-name> [base-branch]
      Create a new worktree for the given branch name.
      Base branch defaults to 'main' if not specified.

  checkout <branch-name>
      Switch to an existing worktree (prints path).

  list
      List all worktrees for current repo.

  remove <branch-name>
      Remove a worktree.

  pr <pr-number>
      Checkout a PR into a new worktree.

USAGE
}

cmd_create() {
    local branch_name="${1:-}"
    local base_branch="${2:-main}"
    
    if [[ -z "$branch_name" ]]; then
        echo "Error: branch name required" >&2
        usage
        exit 1
    fi
    
    local worktree_path="${WORKTREE_BASE}/${branch_name}"
    
    if [[ -d "$worktree_path" ]]; then
        echo "Error: worktree already exists at $worktree_path" >&2
        exit 1
    fi
    
    echo "Creating worktree for branch '$branch_name' based on '$base_branch'..."
    mkdir -p "$(dirname "$worktree_path")"
    
    cd "$REPO_ROOT"
    git worktree add "$worktree_path" -b "$branch_name" "$base_branch"
    
    echo ""
    echo "✓ Worktree created at: $worktree_path"
    echo ""
    echo "To start working:"
    echo "  cd $worktree_path"
}

cmd_checkout() {
    local branch_name="${1:-}"
    
    if [[ -z "$branch_name" ]]; then
        echo "Error: branch name required" >&2
        usage
        exit 1
    fi
    
    local worktree_path="${WORKTREE_BASE}/${branch_name}"
    
    if [[ ! -d "$worktree_path" ]]; then
        echo "Error: worktree does not exist at $worktree_path" >&2
        exit 1
    fi
    
    echo "$worktree_path"
}

cmd_list() {
    cd "$REPO_ROOT"
    echo "Worktrees for ${REPO_NAME}:"
    echo ""
    git worktree list
}

cmd_remove() {
    local branch_name="${1:-}"
    
    if [[ -z "$branch_name" ]]; then
        echo "Error: branch name required" >&2
        usage
        exit 1
    fi
    
    local worktree_path="${WORKTREE_BASE}/${branch_name}"
    
    if [[ ! -d "$worktree_path" ]]; then
        echo "Error: worktree does not exist at $worktree_path" >&2
        exit 1
    fi
    
    cd "$REPO_ROOT"
    
    echo "Removing worktree at: $worktree_path"
    git worktree remove "$worktree_path"
    
    echo ""
    read -p "Delete branch '$branch_name'? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        git branch -D "$branch_name" || echo "Branch may have been already deleted"
    fi
    
    echo "✓ Worktree removed"
}

cmd_pr() {
    local pr_number="${1:-}"
    
    if [[ -z "$pr_number" ]]; then
        echo "Error: PR number required" >&2
        usage
        exit 1
    fi
    
    cd "$REPO_ROOT"
    
    if ! command -v gh &> /dev/null; then
        echo "Error: gh CLI not found" >&2
        exit 1
    fi
    
    echo "Fetching PR #$pr_number info..."
    local branch_name
    branch_name=$(gh pr view "$pr_number" --json headRefName --jq '.headRefName')
    
    if [[ -z "$branch_name" ]]; then
        echo "Error: Could not get branch name for PR #$pr_number" >&2
        exit 1
    fi
    
    local worktree_path="${WORKTREE_BASE}/${branch_name}"
    
    if [[ -d "$worktree_path" ]]; then
        echo "Worktree already exists at: $worktree_path"
        echo "To work on it: cd $worktree_path"
        exit 0
    fi
    
    echo "Creating worktree for PR #$pr_number (branch: $branch_name)..."
    mkdir -p "$(dirname "$worktree_path")"
    
    git fetch origin "$branch_name:$branch_name" 2>/dev/null || true
    git worktree add "$worktree_path" "$branch_name"
    
    echo ""
    echo "✓ Worktree created at: $worktree_path"
    echo ""
    echo "To start working:"
    echo "  cd $worktree_path"
}

main() {
    local command="${1:-}"
    
    if [[ -z "$command" ]]; then
        usage
        exit 1
    fi
    
    shift
    
    case "$command" in
        create) cmd_create "$@" ;;
        checkout) cmd_checkout "$@" ;;
        list) cmd_list "$@" ;;
        remove) cmd_remove "$@" ;;
        pr) cmd_pr "$@" ;;
        help|--help|-h) usage ;;
        *)
            echo "Error: Unknown command '$command'" >&2
            echo "" >&2
            usage
            exit 1
            ;;
    esac
}

main "$@"
