# predd Review Fix (Moonlighting)

## Status: pending

## Problem

When a reviewer leaves comments on a hunter-created PR (proposal or impl), nothing happens. The AI wrote the code but goes deaf once the PR is open. A human has to either fix it themselves or wait for hunter to run a fix loop — which only happens during self-review, not in response to external review comments.

## Solution

predd watches open PRs on hunter-created branches. When it sees new review comments (REQUEST_CHANGES or inline comments) since the last check, it runs a fix skill on the worktree and pushes the result.

This is predd "moonlighting" — it normally only reviews PRs written by others, but here it responds to reviews on PRs it (via hunter) wrote.

## Behaviour

### Trigger

A PR is eligible for fix if:
1. The branch matches `branch_prefix` (i.e. it's a hunter-created branch)
2. There are review comments newer than the last fix attempt (or the PR was never fixed)
3. The PR is open and not merged
4. `review_fix_turns` for this PR < `max_moonlight_turns` (default: 2)

### What it does

1. Finds the local worktree for the branch (searches `worktree_base` for a dir matching the branch name)
2. If no worktree exists, clones a fresh one
3. Builds a prompt:
   ```
   Look at the review comments on PR #{pr_number} in {repo}.
   Fix all requested changes on branch {branch}.
   Workspace: {worktree_path}
   When done, commit your changes and push. Then add a comment to the PR summarising what you changed.
   ```
4. Runs the impl skill with this prompt (same backend as hunter)
5. On completion, records `review_fix_turns += 1` and the SHA of the latest review comment processed
6. If `max_moonlight_turns` exhausted, posts a comment: "I've applied fixes twice — please take another look."

### State

Tracked in `~/.config/predd/state.json` under the PR key, new fields:
- `review_fix_turns: int` — how many fix attempts have been made
- `last_fix_review_sha: str` — ID of the last review comment processed (to avoid re-fixing)

### Config

| Field | Default | Notes |
|-------|---------|-------|
| `moonlight_enabled` | `true` | Enable review fix on hunter PRs |
| `max_moonlight_turns` | `2` | Max fix attempts per PR |
| `moonlight_skill_path` | same as `impl_skill_path` | Skill to use for fixes |

### What it does NOT do

- Does not touch PRs on branches it didn't create (non-`branch_prefix` branches)
- Does not fix reviews on merged PRs
- Does not re-fix the same review comments (tracks last processed review ID)
- Does not run if the PR already has a newer commit pushed after the review (human may have fixed it manually)

## Implementation notes

- New function `moonlight_fix_pr(cfg, state, pr)` in predd.py
- Called from the main poll loop after `process_pr`, only for PRs matching `branch_prefix`
- Uses `gh pr view --json reviews,comments` to get review state
- Worktree lookup: `glob(f"{cfg.worktree_base}/*{branch_slug}*")` — same pattern hunter uses
- If no worktree found, use `setup_new_branch_worktree` to create one from the existing branch
- Prompt includes the full review body + all inline comments pulled via `gh api`
