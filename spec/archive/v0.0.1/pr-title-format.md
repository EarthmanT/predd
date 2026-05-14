# PR Title Format

## Status: pending

## Problem

Hunter currently creates PRs with titles like:
- `Proposal: [DAP09A-1234] Issue name`
- `Implement: [DAP09A-1234] Issue name`
- `impl: [DAP09A-1234] Issue name` (inconsistent casing)

These are harder to scan because the Jira ID is buried inside the prefix. The desired format puts the Jira ID first, making it scannable at a glance.

## Solution

New format: `[JIRA-ID] Proposal/Impl - Issue name`

Rules:
1. If the issue has a Jira key in state (stored as `jira_key`), format: `[{jira_key}] {type} - {issue_title}`
2. If no Jira key, format: `{type} - {issue_title}`
3. `type` is either `Proposal` or `Impl` (capitalized, no colon)
4. `issue_title` is the raw GitHub issue title, stripped of any existing Jira ID prefix like `[DAP09A-XXXX]` to avoid duplication

## Implementation

Add a helper in hunter.py:

```python
def _pr_title(pr_type: str, issue_title: str, jira_key: str | None) -> str:
    """
    pr_type: "Proposal" or "Impl"
    issue_title: raw GitHub issue title
    jira_key: e.g. "DAP09A-1234" or None
    """
    # Strip leading [JIRA-ID] from issue title to avoid duplication
    clean_title = re.sub(r'^\[[A-Z]+-\d+\]\s*', '', issue_title).strip()
    if jira_key:
        return f"[{jira_key}] {pr_type} - {clean_title}"
    return f"{pr_type} - {clean_title}"
```

Use in:
- `process_issue()` when creating the proposal PR body/title
- `create_impl_pr()` (or equivalent) when creating the implementation PR title

The `jira_key` is already stored in hunter state under `issue_data["jira_key"]` when Jira API ingest runs. For non-Jira issues, `jira_key` will be absent/None.

## Test cases

| Input | jira_key | type | Output |
|-------|----------|------|--------|
| `Fix the login bug` | `DAP09A-123` | `Proposal` | `[DAP09A-123] Proposal - Fix the login bug` |
| `[DAP09A-123] Fix the login bug` | `DAP09A-123` | `Impl` | `[DAP09A-123] Impl - Fix the login bug` |
| `Fix the login bug` | None | `Proposal` | `Proposal - Fix the login bug` |
| `[DAP09A-999] Some feature` | None | `Impl` | `Impl - Some feature` |
