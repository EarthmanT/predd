# Decision Logging

## Problem

predd and hunter logs only record high-level events (`Review posted`, `Proposal PR created`). There's no structured record of *why* decisions were made, what was skipped and why, or what conditions were evaluated. This makes it impossible to do automated analysis of patterns.

## Proposed Behaviour

Write a structured JSONL decision log alongside the existing text logs:

- `~/.config/predd/decisions.jsonl` — predd decisions
- `~/.config/predd/hunter-decisions.jsonl` — hunter decisions

Each line is a JSON object with at minimum: `ts`, `event`, and event-specific fields.

## Event Schema

### predd events

```json
{"ts": "2026-05-13T09:00:00Z", "event": "pr_skip", "repo": "owner/repo", "pr": 372, "reason": "already_reviewed_same_sha"}
{"ts": "...", "event": "pr_skip", "repo": "...", "pr": 373, "reason": "draft"}
{"ts": "...", "event": "pr_skip", "repo": "...", "pr": 374, "reason": "own_pr"}
{"ts": "...", "event": "pr_skip", "repo": "...", "pr": 375, "reason": "already_reviewed_by_user"}
{"ts": "...", "event": "pr_review_started", "repo": "...", "pr": 376, "sha": "abc123"}
{"ts": "...", "event": "pr_review_posted", "repo": "...", "pr": 376, "verdict": "REQUEST_CHANGES", "findings_count": 3}
{"ts": "...", "event": "pr_review_failed", "repo": "...", "pr": 376, "error": "timeout"}
```

### hunter events

```json
{"ts": "...", "event": "issue_skip", "repo": "...", "issue": 377, "reason": "already_claimed"}
{"ts": "...", "event": "issue_pickup", "repo": "...", "issue": 377, "title": "..."}
{"ts": "...", "event": "proposal_created", "repo": "...", "issue": 377, "pr": 378}
{"ts": "...", "event": "proposal_merged", "repo": "...", "issue": 377, "pr": 378}
{"ts": "...", "event": "impl_created", "repo": "...", "issue": 377, "pr": 381}
{"ts": "...", "event": "impl_merged", "repo": "...", "issue": 377, "pr": 381}
{"ts": "...", "event": "issue_closed", "repo": "...", "issue": 377}
{"ts": "...", "event": "issue_skip", "reason": "closed_manually", "issue": 323}
{"ts": "...", "event": "skill_no_commits", "issue": 377, "skill": "proposal"}
{"ts": "...", "event": "claim_failed", "issue": 377, "reason": "label_error"}
```

## Implementation

```python
import json

DECISION_LOG = CONFIG_DIR / "decisions.jsonl"
HUNTER_DECISION_LOG = CONFIG_DIR / "hunter-decisions.jsonl"

def log_decision(log_file: Path, event: str, **fields) -> None:
    record = {"ts": _now_iso(), "event": event, **fields}
    with open(log_file, "a") as f:
        f.write(json.dumps(record) + "\n")
```

Call at every decision point. Rotate at 50MB (same as text log).

## JSONL vs SQLite

JSONL is sufficient for now. SQLite makes sense when:
- Analysis queries need to join across events (e.g. time from pickup to close)
- Log grows beyond ~100k events

Migration path: read JSONL into SQLite on demand for analysis; keep JSONL as the write format.
