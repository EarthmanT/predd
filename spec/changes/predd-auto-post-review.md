# Predd: Auto-Post Reviews Without Manual Approval

## Problem

Currently predd saves the review as a draft (`review-summary.md`) and waits for a human to
run `predd approve/comment/request-changes <pr>` before posting. This means reviews sit
unposted until a human intervenes — the GitHub UI never sees them.

## Solution

Auto-post the review immediately after the skill completes. The human can then dismiss,
resolve, or reply to the review directly in the GitHub PR UI. No `predd approve` step needed.

## Behavior Change

**Before:**
1. Skill runs → saves draft to `review-summary.md`
2. Human runs `predd show <pr>` to inspect draft
3. Human runs `predd approve/comment/request-changes <pr>` to post
4. Review appears in GitHub

**After:**
1. Skill runs → review is posted immediately via `gh pr review`
2. Human sees review in GitHub PR UI
3. Human dismisses, resolves, or replies inline as desired

## Implementation

In `process_pr()`, after the skill output is verified non-empty:

1. Extract the verdict (`APPROVE`, `REQUEST_CHANGES`, or `COMMENT`)
2. Extract the review body (everything after the verdict line)
3. Post immediately via `gh pr review`:
   ```python
   gh pr review {pr_number} --repo {repo} --{verdict_flag} --body {body}
   ```
4. Update state to `submitted`
5. Clean up worktree

Remove the `save draft → await manual approval` path entirely. The `predd approve/comment/request-changes` CLI commands can be removed or kept as manual overrides for edge cases.

## Verdict Mapping

| Skill output contains | `gh pr review` flag |
|-----------------------|---------------------|
| `APPROVE`             | `--approve`         |
| `REQUEST_CHANGES`     | `--request-changes` |
| `COMMENT` (or neither)| `--comment`         |

## Failure Handling

If `gh pr review` fails (e.g. can't review own PR, already reviewed):
- Log the error
- Post a comment instead as fallback
- Mark state `submitted` regardless (don't retry review posting)

## Draft Mode (Optional Config)

Add `predd_auto_post = true` (default) so users can opt back into manual approval if desired:

```toml
predd_auto_post = true   # set false to revert to draft-and-approve workflow
```

## Skill Verification

Before running the review skill, validate that the skill file contains instructions for
posting inline comments. Check that the skill body contains at least one of these patterns:

- `gh pr review` (direct CLI call)
- `inline` (reference to inline comments)
- `file:line` or `line comment` (line-level feedback)

If none are present, log a warning:
```
WARNING: Review skill at {path} may not post inline comments.
         Add 'gh pr review --comment --body "..." -F file:line' instructions to the skill.
```

Do not block execution — warn only. The skill may use its own approach.

## Testing

- Test that review is posted immediately after skill completes
- Test verdict extraction maps correctly to gh pr review flags
- Test failure fallback (post comment when review fails)
- Test `predd_auto_post = false` still saves draft
- Test skill validation warns when inline comment keywords are missing
- Test skill validation passes when skill contains expected keywords
