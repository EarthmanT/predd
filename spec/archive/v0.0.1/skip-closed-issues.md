# Hunter: Skip Manually Closed Issues

## Problem

If an issue is closed on GitHub while hunter has it in `proposal_open`, `implementing`, or `self_reviewing` state, hunter keeps polling it indefinitely looking for a merged PR.

## Fix

In the advance-in-flight loop, before checking proposal/impl merge status, verify the issue is still open:

```bash
gh issue view {issue_number} --repo {repo} --json state --jq '.state'
```

If state is `CLOSED`:
- Clean up any worktrees
- Remove hunter labels
- Mark state as `submitted`
- Log: `Issue {key} was closed manually — stopping`

## Where

At the top of the per-key loop in `check_proposal_merged` and `check_impl_merged`. One extra `gh issue view` call per in-flight issue per cycle.
