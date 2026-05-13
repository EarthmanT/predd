# PR Feedback Collection

## Problem

When a human reviews a hunter-created proposal or implementation PR, that feedback is the most valuable signal for improving hunter's skills. Currently it's never captured — hunter doesn't read PR comments, doesn't know if a PR was approved vs rejected vs had changes requested, and doesn't learn from the pattern.

## Proposed Behaviour

Hunter polls its open proposal and impl PRs for review activity and stores the feedback in the decision log and in hunter state.

## What to Collect

For each hunter-owned PR (labeled `sdd-proposal` or `sdd-implementation`):

1. **Review state** — APPROVED / REQUEST_CHANGES / COMMENT
2. **Review body** — the full text of any review
3. **Inline comments** — per-file, per-line review comments
4. **PR comments** — general conversation comments

Stored in hunter state entry as:
```json
{
  "proposal_feedback": [
    {
      "ts": "2026-05-13T10:00:00Z",
      "reviewer": "earthmant",
      "type": "REQUEST_CHANGES",
      "body": "The design section is missing error handling approach.",
      "inline_comments": [
        {"path": "openspec/changes/foo/design.md", "line": 12, "body": "What happens on timeout?"}
      ]
    }
  ]
}
```

## gh API calls

```bash
# Reviews on a PR
gh api repos/{owner}/{repo}/pulls/{pr}/reviews

# Inline review comments
gh api repos/{owner}/{repo}/pulls/{pr}/comments

# General PR comments
gh api repos/{owner}/{repo}/issues/{pr}/comments
```

## Where it runs

In the `check_proposal_merged` poll step — before checking if merged, check for new review activity. Same in `check_impl_ready_for_review`.

Also log to decision log:
```json
{"ts": "...", "event": "pr_feedback", "pr": 378, "issue": 377, "type": "REQUEST_CHANGES", "reviewer": "earthmant", "body": "..."}
```

## Config

```toml
collect_pr_feedback = true
```
