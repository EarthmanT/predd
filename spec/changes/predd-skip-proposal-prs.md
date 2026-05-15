# predd: skip review of proposal PRs

## Problem

predd reviewed proposal PRs #199 and #202, flagging "missing implementation," "missing Helm templates," and "no tests" as critical issues. These are expected absences — a proposal PR contains only a design document, not code. The reviews were noisy, misleading, and appeared on PRs that had already been approved by humans.

## Root cause

predd has no awareness of PR type. It reviews any open non-draft PR it hasn't seen before, regardless of whether it is a proposal or an implementation. The `sdd-proposal` label exists on these PRs but predd does not check for it.

## What to fix

Before queuing a PR for review, check its labels. If the PR has the `sdd-proposal` label, skip it with reason `proposal_pr`.

```python
if "sdd-proposal" in pr_labels:
    log_decision("pr_skip", repo=repo, pr=pr_number, reason="proposal_pr")
    continue
```

### Config option (optional)

Add `review_proposal_prs = false` (default) to allow opting in if someone wants proposal reviews. Default is skip.

### Label source

Labels are already fetched as part of the PR listing payload (`--json labels` is available via `gh pr list`). No extra API call needed.

## What not to change

- Hunter's use of `sdd-proposal` label for discovery — unrelated
- predd's review of `sdd-implementation` PRs — these should still be reviewed

## Tests

- PR with `sdd-proposal` label → skipped, `pr_skip` event logged with reason `proposal_pr`
- PR with `sdd-implementation` label → reviewed normally
- PR with no labels → reviewed normally (existing behaviour)
- PR with both labels (shouldn't happen, but) → skipped (proposal wins)
