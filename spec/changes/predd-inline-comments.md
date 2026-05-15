# predd: post findings as inline PR comments, not review body prose

## Problem

All findings from reviews of #442, #443, #444 were posted as prose in the review body, despite the findings referencing specific files and line numbers. No inline comments were anchored to file/line locations. This makes it hard for authors to act on feedback — they have to cross-reference the review body text against the diff manually.

## What to fix

Parse findings from the skill output and post them as inline review comments via the GitHub PR review API, anchored to the specific file and line.

### Skill output format

The skill already emits a findings table:

```
| # | File | Line | Severity | Category | Issue |
|---|------|------|----------|----------|-------|
| 1 | packages/scanner/sca.py | 42 | critical | security | ... |
```

Parse this table and map each row to an inline comment on the correct file/line.

### GitHub API

Use `gh api` to create a pull request review with inline comments:

```bash
gh api repos/{owner}/{repo}/pulls/{pr}/reviews \
  --method POST \
  --field commit_id=<head_sha> \
  --field event=<APPROVE|REQUEST_CHANGES|COMMENT> \
  --field body=<summary_body> \
  --field "comments[][path]=packages/scanner/sca.py" \
  --field "comments[][line]=42" \
  --field "comments[][body]=Critical: ..."
```

Multiple `comments[]` entries in a single review call.

### Fallback

If a finding references a file or line that doesn't exist in the diff (e.g. out-of-scope files from Bug 3), post it in the review body instead of as an inline comment. Log a warning.

If the findings table is missing or unparseable, fall back to full body review (current behaviour).

### Summary body

Keep a short summary in the review body (verdict, total count by severity). The inline comments carry the detail — the body doesn't need to repeat every finding.

## What not to change

- Skill output format — parse what's already there, don't change the skill
- Verdict logic — inline comments are orthogonal to approve/request-changes decision

## Tests

- Findings table with valid file/line → inline comments created at correct positions
- Finding referencing file not in diff → posted to review body, warning logged
- No findings table in output → full body review, no inline comments
- Mixed: some findings have lines, some don't → inline where possible, body for the rest
