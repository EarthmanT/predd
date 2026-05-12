# Auto-label Unlabelled Proposal and Implementation PRs

## Problem

PRs created manually (not by hunter) don't get `sdd-proposal` or `sdd-implementation` labels. Hunter can't find them, and the repo has no consistent way to identify what stage a PR is at.

## Heuristics

**Obviously a proposal:**
- Title matches `^(Proposal|Propose|SDD|Design|RFC|Spec)[:\ ]` (case-insensitive)
- OR branch name contains `/proposal` or `-proposal`
- Or it add's a proposal to spec changes
- AND not already labeled `sdd-proposal` or `sdd-implementation`

**Obviously an implementation:**
- Title matches `^(Impl|Implement|feat|fix|chore)[:\ ]` (case-insensitive) AND branch contains `-impl` or `/impl`
- OR branch name contains `/impl` or `-impl`
- OR archives a proposal
- AND not already labeled `sdd-proposal` or `sdd-implementation`

## Behaviour

Hunter scans open PRs each poll cycle. For any PR matching the heuristics without the label:
1. Add the appropriate label (`sdd-proposal` or `sdd-implementation`)
2. Log: `Auto-labeled PR #N as sdd-proposal (title match)`

Does not modify PRs that already have either label. Does not create or close anything — label only.

## Where it runs

In hunter's poll loop, after CSV ingest, before issue pickup. Applies to all `hunter_repos`.

## Config

```toml
# Set to false to disable auto-labeling
auto_label_prs = true
```
