# predd: inconsistent triggering — some PRs skipped without recorded reason

## Problem

PR #205 (proposal PR, same batch as #199 and #202) was not reviewed while the other two were. No `pr_skip` decision log entry exists for #205. There is no visible difference between #205 and the reviewed PRs that would explain the skip.

## Root cause (hypothesised)

One of:

1. **State file race** — if predd polls and two PRs are new in the same cycle, but the state file is written after the first review and the process is interrupted before the second, one PR ends up with no state entry and no skip record
2. **Silent exception** — an exception during the PR listing or state-check phase silently drops a PR from the queue without logging a `pr_skip` event
3. **Label/filter edge case** — a transient label state or API pagination edge case causes the PR to not appear in `gh pr list` output

## What to fix

### 1. Log every PR seen, not just every PR reviewed

At the start of each poll cycle, log all PRs returned by `gh pr list` before any filtering. This creates an audit trail — if a PR disappears from subsequent processing, there's a record it was seen.

```python
log_decision("pr_seen", repo=repo, pr=pr_number, head_sha=head_sha)
```

(Low-volume event — one per PR per poll cycle where the PR is open. Can be gated behind a `debug_logging = true` config flag if log volume is a concern.)

### 2. Wrap per-PR processing in a try/except that logs

The per-PR loop should catch all exceptions and log them as `pr_skip` with reason `exception` rather than silently dropping the PR:

```python
try:
    process_pr(repo, pr)
except Exception as e:
    log_decision("pr_skip", repo=repo, pr=pr_number, reason="exception", error=str(e))
    logger.warning("pr %s/%s: exception during processing: %s", repo, pr_number, e)
```

### 3. Verify pagination

Confirm `gh pr list --limit 200` is sufficient for all repos, or make the limit configurable. If a repo has >200 open PRs, newer ones may be silently dropped.

## What not to change

- The actual skip logic (drafts, own PRs, already reviewed) — only the logging coverage

## Tests

- PR processing raises exception → `pr_skip` event logged with reason `exception`, no crash
- All PRs from `gh pr list` appear in logs before filtering
