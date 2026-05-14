# Hunter: Remove Previous Label on State Transition

## Problem

When hunter transitions an issue from `proposal_open` to `implementing`, it adds the `{github_user}:implementing` label but does not remove the `{github_user}:proposal-open` label. This results in issues carrying two hunter labels simultaneously (e.g. `earthmant:proposal-open` + `earthmant:implementing`), which is confusing and breaks the invariant that an issue should have exactly one hunter status label at a time.

Observed in the wild: fusion-e/ai-bp-toolkit#63 has both `earthmant:proposal-open` and `earthmant:implementing`.

The `in_progress` -> `proposal_open` transition correctly removes the old label (hunter.py:740), but the `proposal_open` -> `implementing` transition does not (hunter.py:940-942).

## Root Cause

In `start_implementation()` (hunter.py:940-942), the code adds the `implementing` label but has no corresponding `gh_issue_remove_label()` call for `proposal-open`:

```python
implementing_label = f"{cfg.github_user}:implementing"
gh_ensure_label_exists(repo, implementing_label)
gh_issue_add_label(repo, issue_number, implementing_label)
# Missing: gh_issue_remove_label(repo, issue_number, proposal_open_label)
```

## Proposed Fix

Add a `gh_issue_remove_label` call for the `proposal-open` label immediately after adding the `implementing` label in `start_implementation()`:

```python
implementing_label = f"{cfg.github_user}:implementing"
proposal_label = f"{cfg.github_user}:proposal-open"
gh_ensure_label_exists(repo, implementing_label)
gh_issue_add_label(repo, issue_number, implementing_label)
gh_issue_remove_label(repo, issue_number, proposal_label)
```

This matches the existing pattern used in `process_issue()` at the `in_progress` -> `proposal_open` transition (hunter.py:736-740).

## Scope

Single line addition. No config changes, no state machine changes, no new tests beyond verifying the label removal call.

## Acceptance Criteria

1. After `start_implementation()` runs, the issue has only the `implementing` label (not `proposal-open`)
2. A unit test in `test_hunter.py` mocks `gh_issue_remove_label` and asserts it is called with the `proposal-open` label during the `proposal_open` -> `implementing` transition
3. Existing tests pass: `uv run --with pytest pytest test_hunter.py -q`

## Files Touched

- `hunter.py` — `start_implementation()`: add `gh_issue_remove_label` call
- `test_hunter.py` — new test for label removal on implementing transition
