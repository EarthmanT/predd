# Hunter/Predd: Comment on Failures and Automatic Cleanup

## Problem

When hunter or predd encounters a failure, it currently just logs an error and marks the issue/PR as failed. This leaves the assignee unaware that the tool tried and failed, with no indication on GitHub itself. Additionally, failed entries accumulate indefinitely - failures from weeks ago that were never addressed remain in the state, cluttering `hunter status` output. Common failure modes:

- **Hunter**: Proposal/impl skill produces no commits (AI couldn't figure out what to do)
- **Hunter**: Git push fails (branch protection, conflicts, permissions)
- **Predd**: Review skill fails or produces no output
- **Both**: Skill subprocess crashes or times out

The user only discovers these failures by checking logs or running `hunter status`, which isn't part of the normal GitHub workflow.

## Proposed Behaviour

When hunter or predd encounters a failure, it posts a comment on the GitHub issue or PR explaining what went wrong. This provides visibility directly in the GitHub UI where the user is already working.

Additionally, hunter and predd periodically clean up stale failures (older than 7 days by default). The cleanup re-evaluates whether the failure condition still exists (e.g., did someone manually create the PR? did the issue get closed?) and either archives with a comment or removes the entry entirely. This prevents accumulation of failures that were never addressed.

### Hunter Failure Comments

#### Skill produced no commits

On issue when proposal or impl skill produces no commits:

```
⚠️ Hunter could not create a PR for this issue.

The AI skill ran but produced no git commits. This usually means:
- The issue description is too vague for the AI to understand what to build
- The issue requires context not present in the description
- The skill prompt needs improvement for this type of work

**Issue:** fusion-e/ai-bp-toolkit!377
**Skill:** proposal
**Error:** Proposal skill produced no commits — not creating empty PR

Please either:
1. Add more details to the issue description
2. Create the PR manually
3. Improve the skill prompt at ~/.windsurf/skills/proposal/SKILL.md
```

#### Git push failure

On issue when git push fails after skill produces commits:

```
⚠️ Hunter could not push the PR branch for this issue.

The AI skill produced commits, but git push failed. This usually means:
- Branch protection rules blocking direct pushes
- Branch already exists remotely with conflicts
- Git authentication or permissions issue

**Issue:** fusion-e/ai-bp-toolkit!322
**Branch:** usr/at/322-proposal-dap09a-1794-isv-onboarding-ski
**Error:** Command '['git', 'push', '-u', 'origin', ...] returned non-zero exit status 1

The worktree with commits is preserved at:
/home/adam/windsurf/pr-reviews/fusion-e-ai-bp-toolkit-usr/at/322-proposal-dap09a-1794-isv-onboarding-ski

Please either:
1. Fix branch protection rules to allow pushes from hunter
2. Manually push from the worktree and create the PR
3. Delete the worktree if the work should be discarded
```

#### Skill subprocess crash

On issue when skill subprocess crashes or times out:

```
⚠️ Hunter crashed while processing this issue.

The AI skill subprocess exited unexpectedly.

**Issue:** fusion-e/ai-bp-toolkit!342
**Skill:** proposal
**Exit code:** 1
**Error:** [error message from subprocess]

The worktree is preserved at:
/home/adam/windsurf/pr-reviews/fusion-e-ai-bp-toolkit-usr/at/342-proposal-dap09a-1645-automate-dapo-staging

Please check the logs for details:
tail -f ~/.config/predd/hunter-log.txt
```

### Predd Failure Comments

#### Review skill produces no output

On PR when review skill produces no output:

```
⚠️ Predd could not review this PR.

The AI review skill ran but produced no review output.

**PR:** fusion-e/ai-bp-toolkit#382
**Error:** Review skill produced no output

Please either:
1. Review this PR manually
2. Check the skill prompt at ~/.windsurf/skills/pr-review/SKILL.md
3. Check logs: tail -f ~/.config/predd/log.txt
```

#### Review skill crashes

On PR when review skill crashes or times out:

```
⚠️ Predd crashed while reviewing this PR.

The AI review subprocess exited unexpectedly.

**PR:** fusion-e/ai-bp-toolkit#382
**Exit code:** 1
**Error:** [error message from subprocess]

Please check the logs for details:
tail -f ~/.config/predd/log.txt
```

## Config

```toml
# If false, failures are only logged (current behavior)
comment_on_failures = true

# Label to add when hunter fails on an issue
failure_label = "{github_user}:hunter-failed"

# Label to add when predd fails on a PR
predd_failure_label = "{github_user}:predd-failed"

# Automatically clean up failures older than this many days (0 = disable)
failure_cleanup_days = 7

# How often to run failure cleanup (in poll cycles, 0 = startup only)
failure_cleanup_interval = 10
```

## Failure Cleanup

Hunter and predd should periodically clean up old failures to prevent accumulation of stale entries that were never addressed.

### Cleanup Behavior

Every `failure_cleanup_interval` poll cycles (default: 10), hunter and predd scan their failed entries and:

1. **Check age**: If `first_seen` is older than `failure_cleanup_days` (default: 7 days), consider it stale
2. **Re-evaluate condition**: Check if the failure condition still exists
3. **Archive or remove**: Either archive with a comment or remove entirely

### Hunter Failure Re-evaluation

For each failed issue older than the threshold:

**Skill no commits failure:**
- Check if a PR now exists for this issue (search for PRs referencing the issue number)
- If PR exists: remove failure entry, add comment "Archiving failure - PR now exists"
- If no PR: remove failure entry, add comment "Archiving stale failure - no action taken in 7 days, please retry manually if still relevant"

**Git push failure:**
- Check if the branch now exists remotely
- If branch exists: remove failure entry, add comment "Archiving failure - branch now exists"
- If branch doesn't exist: delete local worktree, remove failure entry, add comment "Archiving stale failure - worktree cleaned up, please retry if still relevant"

**Skill crash failure:**
- Check if issue is still open and assigned to the user
- If issue closed or reassigned: remove failure entry (no comment needed)
- If still open: remove failure entry, add comment "Archiving stale failure - please retry manually if still relevant"

### Predd Failure Re-evaluation

For each failed PR older than the threshold:

**Review skill failure:**
- Check if PR is still open
- If PR closed: remove failure entry (no comment needed)
- If PR still open: remove failure entry, add comment "Archiving stale failure - PR still open, please review manually"

### Cleanup Comments

When archiving a failure with a comment, use a consistent format:

```
🧹 Hunter archiving stale failure

This failure was not addressed within 7 days and is being archived.

**Original issue:** fusion-e/ai-bp-toolkit!322
**Original failure:** Git push failed
**Failed on:** 2026-05-12

If this issue is still relevant, please retry manually or improve the skill prompt.
```

### Implementation Notes

1. Add `cleanup_stale_failures()` function in both hunter.py and predd.py
2. Call this function in the main poll loop, respecting `failure_cleanup_interval`
3. Use `first_seen` timestamp from state entries to determine age
4. For git push failures, clean up worktrees with `git worktree remove --force`
5. Log decision event `failure_archived` with issue/pr/repo/reason
6. Remove failure labels when archiving

## Implementation Notes

### Hunter

1. In `process_issue()`, catch exceptions from skill execution:
   - `RuntimeError("Proposal skill produced no commits")` → post comment
   - `subprocess.CalledProcessError` on git push → post comment with worktree path
   - `subprocess.TimeoutExpired` → post comment with timeout info
   - Generic `Exception` → post comment with error message

2. After posting comment, add failure label and mark state as `failed`

3. Preserve worktree on push failures so user can recover the commits

4. Use existing `gh_issue_comment()` helper for posting comments

5. Log decision event `failure_commented` with issue/repo/error type

6. In main poll loop, call `cleanup_stale_failures()` every `failure_cleanup_interval` cycles
   - Similar to existing orphan scan pattern
   - Track cleanup cycle counter in state or module variable

### Predd

1. In PR review flow, catch exceptions from skill execution:
   - Empty review output → post comment
   - `subprocess.TimeoutExpired` → post comment
   - Generic `Exception` → post comment

2. After posting comment, add failure label and mark state as `failed`

3. Use existing `gh_pr_comment()` helper for posting comments

4. Log decision event `pr_review_failed` with PR/repo/error type

5. In main poll loop, call `cleanup_stale_failures()` every `failure_cleanup_interval` cycles
   - Similar to existing orphan scan pattern
   - Track cleanup cycle counter in state or module variable

### Error Message Templates

Store comment templates as constants at the top of each file for easy editing:

```python
_HUNTER_NO_COMMITS_COMMENT = """⚠️ Hunter could not create a PR for this issue.
..."""

_HUNTER_PUSH_FAILURE_COMMENT = """⚠️ Hunter could not push the PR branch for this issue.
..."""
```

### Retry Behavior

After posting a failure comment:
- Hunter: Issue remains in `failed` state, not retried automatically
- User can manually retry by running `hunter rollback <issue>` then letting hunter pick it up again
- Predd: PR remains in `failed` state, not retried automatically
- User can manually retry by running `predd retry <pr>` (new command)

## Success Criteria

1. When hunter fails on an issue, a comment appears on the issue explaining what went wrong
2. When predd fails on a PR, a comment appears on the PR explaining what went wrong
3. Worktree paths are included in comments for push failures so user can recover work
4. Failure labels are applied for easy filtering
5. Decision logs record the failure comment event
6. Config option to disable failure commenting (fallback to current behavior)
7. Failures older than `failure_cleanup_days` are automatically cleaned up
8. Cleanup re-evaluates whether the failure condition still exists before removing
9. Worktrees are deleted when archiving git push failures
10. Cleanup comments are posted when archiving failures (unless issue/PR is closed)
