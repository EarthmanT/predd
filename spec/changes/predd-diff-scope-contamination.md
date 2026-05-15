# predd: out-of-scope files contaminating PR review findings

## Problem

Three impl PRs (#442, #443, #444) all had findings referencing `.windsurf/skills/security-threat-assessment/extract-threats.py` with nearly identical warnings (path traversal ordering, validate_threat naming, error message sanitization). This file is unrelated to any of the three PRs' stated purposes (SCA scanning, NetworkPolicy, image signing).

The agent is diffing against a stale base and picking up accumulated changes from other branches or squashed commits, pulling unrelated files into scope.

## Root cause

The skill fetches the PR diff via `gh pr diff` or similar. If the base branch has diverged from the PR's merge base (e.g. the PR was rebased, or the base has moved since the PR was opened), the diff can include changes that accumulated on the base branch since the PR branched off — or changes from other merged PRs that haven't been cleaned from the working tree.

The skill prompt does not instruct the model to restrict findings to files that are explicitly part of the PR's stated purpose.

## What to fix

### 1. Use merge-base diff

Fetch the diff using the PR's merge base, not the current base branch HEAD:

```bash
gh pr diff <pr> --patch   # always diffs against merge base — use this
# NOT: git diff main...HEAD (stale if base has moved)
```

Verify that `gh pr diff` uses the correct merge base. If not, compute it explicitly:

```bash
base_sha=$(gh pr view <pr> --json baseRefOid --jq .baseRefOid)
head_sha=$(gh pr view <pr> --json headRefOid --jq .headRefOid)
git diff $base_sha...$head_sha
```

### 2. Pass explicit file list to skill

Extract the list of changed files from the diff and pass it to the skill prompt:

```
Files changed in this PR:
- packages/scanner/sca.py
- packages/scanner/tests/test_sca.py

Only report findings for these files. Do not comment on files not in this list.
```

This prevents the model from referencing files it sees in the broader repo context that aren't in the PR.

### 3. Skill prompt guard

Add an explicit instruction to the skill: "Only flag issues in files that appear in the diff. Do not flag issues in files that are present in the repository but not changed by this PR."

## What not to change

- The diff fetching mechanism if `gh pr diff` already uses merge base correctly — only fix if confirmed broken
- Skill files for repos we don't control

## Tests

- Diff includes only files A and B; review findings reference only A and B — no findings for file C
- PR diff fetched via merge base matches expected changed file list
- Skill prompt includes explicit changed file list
