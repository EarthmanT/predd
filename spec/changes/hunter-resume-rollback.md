# Hunter: Resume and Rollback

## Problem

When hunter crashes mid-step (signal, error, machine restart), it leaves orphaned state:
- GitHub label applied with no PR behind it
- Worktree on disk with partial work
- State file showing a non-terminal status that blocks retry

On restart, hunter skips the issue (already `in_progress`) and never cleans up.

## Proposed Behaviour

### Resume

On each poll cycle, before picking up new issues, hunter inspects all issues in non-terminal states and attempts to resume them from their last known checkpoint.

**Checkpoint = workflow step boundary.** Each step writes a checkpoint to hunter state before doing external work. On resume, hunter reads the checkpoint and re-enters the workflow at that step.

| State | Resume action |
|-------|--------------|
| `in_progress` | Worktree exists with commits → advance to `proposal_open` step. Worktree empty or missing → rollback to unclaimed. |
| `proposal_open` | PR exists on GitHub → nothing to do, wait for merge. PR missing → re-run proposal step from existing worktree if present, else rollback. |
| `implementing` | Worktree exists → re-run impl skill if no commits since proposal merge. PR exists → wait for review. |
| `self_reviewing` | Resume review loop at `review_loops_done` count stored in state. |
| `ready_for_review` | PR exists → wait for merge. PR missing → rollback to `implementing`. |

### Rollback

When a step cannot be resumed (worktree missing, skill output unrecoverable, max retries exceeded), hunter rolls back to the nearest clean checkpoint:

1. Delete worktree if it exists
2. Remove all `{user}:*` labels from the issue
3. Delete the state entry for this issue
4. Log the rollback clearly

The issue will be picked up fresh on the next poll cycle.

### Orphaned Label Detection

On startup, hunter scans for issues labeled `{user}:in-progress` that have no matching entry in hunter state. These are orphans from crashed runs. Hunter rolls them back immediately (remove label).

## Implementation Notes

### Checkpoint writes

Every step that does external work (gh api calls, skill runs, git ops) must write its checkpoint to state **before** starting:

```python
update_issue_state(state, key, status="in_progress", worktree=str(wt_path))
# ^ written before skill runs, so resume knows worktree path
```

### Resume logic location

Add `resume_in_flight_issues(cfg, state)` called at the top of each poll cycle before the main issue scan.

### Worktree inspection

To determine how far a step got:
```python
def worktree_has_commits_since(worktree: Path, base_branch: str) -> bool:
    result = subprocess.run(
        ["git", "log", f"origin/{base_branch}..HEAD", "--oneline"],
        capture_output=True, text=True, cwd=str(worktree),
    )
    return bool(result.stdout.strip())
```

### Max retries

Add `max_resume_retries: int` config option (default: 2). If hunter has attempted to resume an issue more than this many times, roll it back completely and notify via toast/log.

## Config

```toml
max_resume_retries = 2
```

## What Does NOT Change

- The workflow steps themselves are unchanged
- Proposal and impl skills are re-run idempotently (openspec handles existing changes)
- The `proposal_open` / `implementing` / etc. wait states are unaffected — resume is only relevant for steps that were mid-execution
