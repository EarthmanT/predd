# Skip Already-Reviewed PRs

## Problem

predd only checks its own `state.json` to decide whether to review a PR. It has no awareness of GitHub's actual state. This means it will queue a review for:

- PRs that are already merged or closed
- PRs the user has already reviewed manually on GitHub
- PRs that already have a review from the configured `github_user`

## Proposed Behaviour

Before processing a PR, fetch its review state from GitHub and skip if:

1. PR state is `MERGED` or `CLOSED`
2. The configured `github_user` has already submitted a review (any verdict)

## Implementation Notes

Use `gh pr view` with `--json state,reviews` to get both in one call:

```bash
gh pr view <number> --repo <owner/repo> --json state,reviews
```

Response shape:
```json
{
  "state": "OPEN",
  "reviews": [
    { "author": { "login": "adamuser" }, "state": "APPROVED" }
  ]
}
```

Skip conditions:
- `state` is not `"OPEN"` → skip, mark as `rejected` in local state
- any entry in `reviews` where `author.login == github_user` → skip, mark as `rejected`

## Where to Add It

In the poll loop, after the existing draft/SHA checks and before calling `process_pr`. Single `gh pr view` call per candidate PR.

## Tradeoff

One extra `gh` call per unreviewed PR per poll cycle. At 90s intervals with ~10 open PRs this is negligible.
