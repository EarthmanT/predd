# predd: review output sanitization and double-post prevention

## Problem

Two related bugs observed on PRs #443, #444:

**Bug A — Terminal output posted as a second review**
After posting the real review, the skill subprocess emits a second chunk of output (terminal summary, including the literal text "Perfect! The review has been posted. Now let me print the terminal summary:"). predd posts this as a second GitHub review, submitted as `CHANGES_REQUESTED`. Because this second review is more recent, it becomes the authoritative blocking state on the PR, superseding the real review.

**Bug B — Preamble in review body**
The skill leaks internal monologue into the review body — raw JSON verdict blocks, terminal-style output, prose like "I've completed the review." These appear verbatim on GitHub.

Both root causes are the same: predd passes the full raw stdout of the skill subprocess to GitHub with no parsing or deduplication.

## Root cause

1. **No output parser** — predd uses the raw skill output as the review body verbatim
2. **Multiple `gh pr review` calls not guarded** — if the skill somehow invokes the review posting internally (or produces output that predd misinterprets as a second review event), predd does not check whether a review has already been posted for the current head SHA before posting again

## What to fix

### 1. Parse skill output before posting

The review skill emits a JSON verdict block followed by a markdown summary. Extract these before posting:

1. Find the last fenced ` ```json ` block — extract `verdict` from it
2. Find the first `## ` heading — use everything from there to end of output as the review body
3. If no JSON block: fall back to full output, log a warning, use `COMMENT` verdict
4. If no `## ` heading: use full output, log a warning
5. Strip trailing whitespace

Verdict mapping:

| JSON `verdict` | GitHub event |
|----------------|-------------|
| `APPROVE` | `APPROVE` |
| `REQUEST_CHANGES` | `REQUEST_CHANGES` |
| `COMMENT` | `COMMENT` |
| missing / unparseable | `COMMENT` (safe default) |

### 2. One review per head SHA, enforced in predd

Before posting any review, check whether predd has already posted a review for the current head SHA. If yes, skip — do not post a second review regardless of what the skill returned.

This check already partially exists (the `submitted` + `head_sha` state), but it must be enforced *within* the posting call, not just at poll time. If the skill subprocess somehow triggers a second post, the guard must catch it.

```python
if state.get("last_posted_sha") == current_head_sha:
    logger.warning("review already posted for sha %s — skipping duplicate post", current_head_sha)
    return
```

### 3. Write `last_posted_sha` atomically before posting

Write `last_posted_sha = head_sha` to state *before* calling the GitHub API. This prevents a retry loop from posting twice if the process crashes mid-post.

## What not to change

- Skill files themselves — fix is in predd's output handling
- Decision log schema — `pr_review_posted` already records the verdict

## Tests

- Output with preamble + JSON block + `##` body → preamble stripped, body starts at `##`
- Output with no JSON block → full output used, `COMMENT` verdict, warning logged
- Output with no `##` heading → full output used, warning logged
- Second call to post review for same head SHA → skipped, warning logged
- Malformed JSON block → `COMMENT` verdict, warning logged
