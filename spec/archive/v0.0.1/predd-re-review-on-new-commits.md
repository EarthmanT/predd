# Fix: predd Re-reviews PRs on New Commits

## Problem

predd stores `head_sha` per PR and skips any PR with status `submitted` and the same SHA. Once a PR is reviewed, it is never reviewed again — even if the author pushes new commits.

## Fix

Change the skip condition: skip only if `head_sha` matches AND status is `submitted`. If the SHA has changed, treat as a new review regardless of prior status.

This is already how the logic works for `status == "failed"` — it retries on new SHA. Apply the same to `submitted`.

### Current skip logic

```python
if entry_sha == head_sha and entry_status in (
    "submitted", "rejected", "awaiting_approval", "reviewing"
):
    continue
```

### Fixed skip logic

```python
if entry_sha == head_sha and entry_status in (
    "rejected", "awaiting_approval", "reviewing"
):
    continue
# submitted with same SHA → skip
if entry_sha == head_sha and entry_status == "submitted":
    continue
# submitted with new SHA → re-review (fall through)
```

Which simplifies to: remove `"submitted"` from the skip list. The existing SHA check already handles it — if SHA changes, it falls through regardless of status.

```python
if entry_sha == head_sha and entry_status in (
    "rejected", "awaiting_approval", "reviewing"
):
    continue
```

## Behaviour After Fix

- PR reviewed → status `submitted`, SHA stored
- Author pushes new commit → new SHA
- Next poll: SHA mismatch → re-review
- PR reviewed again → status `submitted`, new SHA stored
- No new commits → skip (same SHA, status `submitted` is now not in skip list but SHA matches → still skips via the `reviewing`/`awaiting_approval` path... wait)

Actually the correct fix is simpler: **only skip `submitted` if SHA matches**. The current code already does this implicitly — the SHA check comes first. So the fix is just removing `"submitted"` from the terminal skip list. `rejected` stays (user explicitly discarded it).

## Updated skip list

```python
SKIP_STATUSES = {"rejected", "awaiting_approval", "reviewing"}
```

`submitted` is removed — predd will re-review on new commits automatically.
