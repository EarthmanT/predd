# Hunter: Close Issue When Implementation PR Merges

## Problem

When an implementation PR merges, hunter currently reopens the issue and reassigns it to the reporter for "verification". This is too optimistic — it creates noise, adds labels, and assumes a verification workflow that doesn't exist in practice. Issues end up stuck open with stale labels.

The right behaviour: when the impl PR merges, close the issue with a comment linking to the PR.

## Proposed Behaviour

When hunter detects an impl PR has merged:

1. Post a comment on the issue: `Implemented in #{impl_pr}. Closing.`
2. Close the issue via `gh issue close`
3. Update hunter state to `status: submitted`

That's it. No reopening, no reassigning, no awaiting-verification label.

## What Goes Away

- `awaiting_verification` state
- `{github_user}:awaiting-verification` label
- `gh_issue_reopen_and_reassign` call in `check_impl_merged`

## Implementation Notes

Replace the body of `check_impl_merged` after detecting the merged PR:

```python
gh_issue_comment(repo, issue_number,
    f"Implemented in #{impl_pr}. Closing.")
gh_run(["issue", "close", str(issue_number), "--repo", repo])
update_issue_state(state, key, status="submitted")
```

Remove `awaiting_verification` from `TERMINAL_STATES` and replace with `submitted` (already in there for predd, reuse it).

## Config

No new config — this is the only sensible behaviour.
