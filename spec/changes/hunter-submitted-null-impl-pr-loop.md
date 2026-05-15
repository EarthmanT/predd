# hunter: submitted state with null impl_pr causes infinite rollback loop

## Problem

When a hunter issue reaches `submitted` status but `impl_pr` is null in state, the resume/reconcile scan misreads it as an incomplete in-flight issue and attempts recovery every poll cycle. Each attempt exceeds `max_resume_retries`, triggers a rollback, resets `resume_attempts` to 0, and the next cycle starts over. The issue loops indefinitely and is never treated as terminal.

Observed on fusion-e/ai-bp-toolkit#331 (DAP09A-1781): impl PR #420 merged on 2026-05-15, but hunter never recorded `impl_pr`. The state read `{"status": "submitted", "impl_pr": null}` and triggered hundreds of rollback/reconcile cycles over ~14 hours.

## Root cause

Two separate gaps:

1. **`submitted` is not truly terminal in the reconcile guard** — `resume_in_flight_issues()` skips `submitted` entries, but the reconcile path (`reconcile_assigned_issues`) re-injects them into active tracking if it sees the issue is still open on GitHub. A `submitted` entry with `impl_pr: null` re-enters the resume loop.

2. **`impl_pr` not recorded before status is set to `submitted`** — if the issue-close step runs but fails partway through (e.g. the GitHub issue close call errors after state is written), `impl_pr` may never be persisted even though the PR merged.

## What to fix

### 1. Guard submitted state in reconcile

In `reconcile_assigned_issues()`, skip entries where `status == "submitted"` regardless of whether the GitHub issue is open. `submitted` means hunter considers its work done — it should not re-enter the loop.

```python
if entry.get("status") in ("submitted", "merged", "awaiting_verification"):
    continue
```

### 2. Record impl_pr before closing the issue

In the transition that sets `status = "submitted"`, write `impl_pr` to state *before* closing the GitHub issue. If the close call fails, the state still has the correct `impl_pr` and the next cycle can detect the merged PR and close cleanly rather than treating the entry as unfinished.

### 3. Detect and self-heal submitted+null impl_pr on startup

In `resume_in_flight_issues()`, add a check: if `status == "submitted"` and `impl_pr is None`, log a warning and skip rather than attempting resume. These entries are unrecoverable without manual intervention — attempting a rollback makes things worse.

```python
if entry["status"] == "submitted" and not entry.get("impl_pr"):
    logger.warning(
        "issue %s is submitted but impl_pr is null — skipping resume (manual check needed)",
        key,
    )
    continue
```

## What not to change

- The rollback logic itself — it is correct for genuinely stuck in-progress issues
- The `max_resume_retries` config — this is a separate concern
- State file schema — no new fields needed

## Tests

- `reconcile_assigned_issues`: skips entries with `status == "submitted"` even if GitHub issue is open
- `resume_in_flight_issues`: logs warning and skips `submitted` + `impl_pr: null` entries instead of retrying
- Impl_pr written to state before `gh issue close` is called — simulate close failure, verify state still has impl_pr on next resume
