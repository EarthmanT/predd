# Hunter: Max 1 New Issue Pickup Per Repo Per Cycle

## Problem

Hunter processes every newly assigned issue in a single poll cycle, creating proposal PRs for all of them simultaneously. This floods the repo with PRs and overwhelms the skill backend.

## Fix

In the issue pickup loop, stop after picking up 1 new issue per repo per poll cycle. Already-in-flight issues (advancing through `proposal_open` → `implementing` etc.) are not affected — only the initial pickup of new issues is capped.

## Implementation

Add a counter per repo in the poll loop:

```python
new_issues_this_cycle = 0
for issue in issues:
    ...
    if status == "":
        if new_issues_this_cycle >= 1:
            continue
        process_issue(...)
        new_issues_this_cycle += 1
```

## Config

```toml
max_new_issues_per_cycle = 1  # per repo
```

Default: 1.
