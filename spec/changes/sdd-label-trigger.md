# Fix: sdd-proposal Label as Proposal Trigger

## Problem

Hunter finds its own proposal PRs by a body marker `hunter:issue-N`. This breaks when:
- A proposal PR was created outside hunter (no marker)
- The marker is missing or wrong
- State stores the wrong PR number

Result: hunter never detects a merged proposal and never starts implementation.

## Fix

### PR Labels

Hunter applies labels to PRs it creates:
- `sdd-proposal` on proposal PRs
- `sdd-implementation` on impl PRs

### Trigger: Start Implementation

Instead of checking a stored `proposal_pr` number, hunter scans for merged PRs labeled `sdd-proposal` whose title or body references the issue number:

```bash
gh pr list --repo {repo} --state merged --label sdd-proposal \
  --json number,title,body,mergedAt
```

Filter: title contains `[{jira_key}]` OR body contains `#{issue_number}`.

This works whether the PR was created by hunter or a human.

### State Changes

- Remove `proposal_pr` from state — it was only needed to track the PR for merge detection
- `proposal_open` state is now determined by: issue has `[github_user]:proposal-open` label AND no merged `sdd-proposal` PR found yet
- On detecting a merged `sdd-proposal` PR, advance to `implementing`

### Ensure Labels Exist

Before creating PRs, ensure `sdd-proposal` and `sdd-implementation` labels exist in the repo:

```bash
gh label create sdd-proposal --repo {repo} --color 0075ca --force
gh label create sdd-implementation --repo {repo} --color e4e669 --force
```

## Implementation Notes

- `gh_create_branch_and_pr` in hunter: add `--label sdd-proposal` or `--label sdd-implementation` parameter
- New helper `gh_find_merged_proposal(repo, issue_number, jira_key)` — scans merged `sdd-proposal` PRs
- Poll loop: replace `check_proposal_merged` logic with label-based scan
- Existing `proposal_pr` entries in state are ignored (treated as stale)
