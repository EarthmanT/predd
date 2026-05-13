# obsidian observe

## What It Does

Runs hourly. Reads new activity from GitHub (PR reviews, inline comments, PR comments) across all watched repos since the last run. Writes one Obsidian note per PR and one per issue that had new activity.

## Vault Location

`~/.config/predd/obsidian/`

## Note Format

### PR observation note
Path: `observations/YYYY-MM-DD-pr-{number}.md`

```markdown
---
type: pr-observation
pr: 378
issue: 377
repo: fusion-e/ai-bp-toolkit
title: "Proposal: [DAP09A-1184] TOON"
label: sdd-proposal
observed_at: 2026-05-13T10:00:00Z
---

## Reviews

### REQUEST_CHANGES — earthmant (2026-05-13T10:00:00Z)
The design section is missing error handling approach.

**Inline comments:**
- `openspec/changes/toon/design.md:12` — What happens on timeout?
- `openspec/changes/toon/tasks.md:5` — Task 3 is too vague

## Comments

- **earthmant** (2026-05-13T10:05:00Z): Also check the spec for the existing MCP timeout handling
```

### Issue observation note
Path: `observations/YYYY-MM-DD-issue-{number}.md`

```markdown
---
type: issue-observation
issue: 377
repo: fusion-e/ai-bp-toolkit
title: "[DAP09A-1184] TOON"
status: proposal_open
observed_at: 2026-05-13T10:00:00Z
---

## Current State
Proposal PR #378 open. 1 REQUEST_CHANGES review.

## Feedback Summary
- Missing error handling in design
- Task descriptions too vague

## Related
- [[observations/2026-05-13-pr-378]]
```

## Tracking Last Run

Stores last run timestamp in `~/.config/predd/obsidian/.last-observe`. Only fetches activity since that timestamp.

## CLI

```bash
obsidian observe [--since "2026-05-01"]   # default: since last run
obsidian observe --dry-run                 # print what would be written, don't write
```

## Implementation Notes

- Reads PR feedback already collected in `hunter-state.json` — no new GitHub API calls needed for hunter PRs
- For predd-reviewed PRs, reads `decisions.jsonl` for `pr_review_posted` events
- Creates vault dirs if they don't exist
- Appends to existing note if PR was observed before (preserves history)
- Uses `---` frontmatter for Obsidian metadata/search
