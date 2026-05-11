# Configurable Trigger Mode

## Problem

predd currently reviews every open non-draft PR it hasn't seen. This is noisy — it reviews PRs nobody asked it to review, including ones still being worked on.

## Proposed Behaviour

Add a `trigger` config option with two modes:

**`trigger = "ready"`** (current behaviour)
Review any open, non-draft PR not yet seen. Good for small teams where every PR should be reviewed.

**`trigger = "requested"`**
Only review PRs where the configured `github_user` has been explicitly added as a reviewer. Good for larger teams or when you want opt-in reviews.

## Config

```toml
# "ready" — review all open non-draft PRs (default)
# "requested" — only review PRs where you are a requested reviewer
trigger = "ready"
```

## Implementation Notes

`gh pr list` already supports filtering by review requests:

```bash
gh pr list --repo owner/repo --state open \
  --json number,title,author,headRefOid,headRefName,isDraft,reviewRequests
```

`reviewRequests` is a list of `{ login }` objects. In `requested` mode, skip any PR where `github_user` is not in that list.

No extra API calls needed — just an additional filter on the existing `gh pr list` payload.

## Behaviour at Mode Switch

Changing `trigger` does not reset state. PRs already `submitted` stay submitted. Only affects which new PRs get picked up going forward.
