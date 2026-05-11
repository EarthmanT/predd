# Fix: Hunter Must Not Create PRs Without Proposal Content

## Problem

Hunter creates proposal PRs immediately after claiming an issue, before the proposal skill has run and committed anything. The flow is:

1. Claim issue, create worktree
2. Run proposal skill (may take minutes)
3. **Create PR** ← happens even if skill produced no commits

The `gh_create_branch_and_pr` function creates an empty commit (`chore: open branch`) to satisfy GitHub's requirement that a branch diverge from base before a PR can be opened. This results in PRs with 0 changed files — just a placeholder commit, no actual proposal content.

## Root Cause

`gh_create_branch_and_pr` is called in `process_issue` regardless of whether the skill committed anything. The empty commit workaround masks the underlying issue.

## Fix

1. **Run the proposal skill before creating the PR.** (Already the case — skill runs first.)
2. **Check for real commits before creating the PR.** After the skill runs, verify there are commits beyond the empty branch-open commit. If not, fail loudly instead of creating an empty PR.
3. **Remove the empty commit workaround.** Instead, require the skill to commit something. If the skill produces no commits, mark the issue as `failed` with a clear log message: `"Proposal skill produced no commits for {key} — not creating empty PR"`.

## Behaviour After Fix

- Skill runs, commits proposal artifacts → PR created with real content
- Skill runs but commits nothing → issue marked `failed`, no PR created, worktree cleaned up
- On next poll, resume logic retries (up to `max_resume_retries`)

## Implementation Notes

After `run_skill(cfg, cfg.proposal_skill_path, ...)` returns, check:

```python
result = subprocess.run(
    ["git", "log", "--oneline", "HEAD", "--not", "--remotes"],
    capture_output=True, text=True, cwd=str(worktree),
)
if not result.stdout.strip():
    raise RuntimeError("Proposal skill produced no commits — not creating empty PR")
```

Remove the `git commit --allow-empty -m "chore: open branch"` from `gh_create_branch_and_pr`. The branch is already created with a real commit from the worktree setup (`git worktree add -b {branch}`), and the skill's commits are what diverge from base.

Wait — `git worktree add -b {branch}` does not create a commit. The branch starts at `origin/base` with no new commits. The first real commit must come from the skill.

So the fix is: drop the empty commit, check for skill commits, fail if none.
