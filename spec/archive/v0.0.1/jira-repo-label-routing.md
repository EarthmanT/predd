# Jira Repo Label Routing

## Problem

Jira API integration currently creates issues in all configured GitHub repos without considering which repo each issue belongs to. A single Jira issue may not be relevant to all repos, causing unnecessary duplication and noise.

## Solution

Add label-based routing: only create a GitHub issue in a repo if the corresponding Jira issue has a label matching the repo slug format `[org/repo]`.

## Behavior

When `ingest_jira_api()` processes a Jira issue:

1. Extract issue labels from Jira API response
2. For each configured repo (e.g., `fusion-e/ai-bp-toolkit`):
   - Check if issue has label matching that repo slug
   - Only create GitHub issue in that repo if label is present
3. If issue has no matching labels, skip it entirely (no issues created in any repo)
4. If issue has some matching labels, create issues only in those repos

## Configuration

No new config options required. Uses existing `repos` list from config.

Example:
```toml
repos = ["fusion-e/ai-bp-toolkit", "fusion-e/other-repo"]
```

## Jira Label Format

Labels on Jira issues use exact repo slug matching:
- Repo: `fusion-e/ai-bp-toolkit` → label must be `fusion-e/ai-bp-toolkit`
- Repo: `fusion-e/other-repo` → label must be `fusion-e/other-repo`

Labels are case-sensitive and must match exactly.

## API Details

- Jira API returns labels in issue.fields.labels array
- Example response:
  ```json
  {
    "key": "DAP-1",
    "fields": {
      "summary": "Fix bug",
      "labels": ["fusion-e/ai-bp-toolkit", "tech-debt"]
    }
  }
  ```

## Implementation

Update `ingest_jira_api()` in hunter.py:

```python
def ingest_jira_api(cfg: Config, repos: list[str]) -> None:
    # ... existing auth/search logic ...

    for issue in issues:
        # ... existing validation (type, sprint) ...

        # NEW: Extract labels and filter repos
        issue_labels = issue.get("fields", {}).get("labels", [])
        matching_repos = [r for r in repos if r in issue_labels]

        if not matching_repos:
            log_decision(
                "api_issue_skip",
                jira_key=jira_key,
                reason="no_matching_repo_labels",
                labels=issue_labels,
            )
            continue

        # ... build issue body ...

        # Create only in matching repos
        for repo in matching_repos:
            # ... existing issue creation logic ...
```

## Labels Config

Update JiraClient to fetch labels field:

```python
fields=[
    "key", "summary", "status", "issuetype",
    "customfield_10005",  # Epic link
    "customfield_10006", "customfield_10007", "customfield_10008",  # Sprint variants
    "labels",  # NEW: for repo routing
]
```

## Testing

- Test skips issue with no labels
- Test skips issue with unmatched labels
- Test creates in repos with matching labels only
- Test creates in multiple repos if multiple labels present
- Test case sensitivity

## Backward Compatibility

This is a breaking change:
- **Before:** Creates issues in all configured repos
- **After:** Creates issues only in repos with matching labels

Existing Jira issues without repo labels will not be ingested. Migration path:
1. Add repo labels to existing Jira issues
2. Re-run ingest

## Out of Scope

- Automatic label creation based on issue content
- Prefix matching (e.g., `fusion-e/*`)
- Label aliases or mappings
- Dynamic repo discovery from labels

## Example Workflow

```
Config repos: ["fusion-e/ai-bp-toolkit", "fusion-e/other-repo"]

Jira issue DAP-1234:
  labels: ["fusion-e/ai-bp-toolkit", "high-priority"]
  → Creates issue only in fusion-e/ai-bp-toolkit

Jira issue DAP-5678:
  labels: ["fusion-e/other-repo"]
  → Creates issue only in fusion-e/other-repo

Jira issue DAP-9999:
  labels: ["shared-work", "high-priority"]
  → Skips (no matching repo labels)

Jira issue DAP-1111:
  labels: ["fusion-e/ai-bp-toolkit", "fusion-e/other-repo"]
  → Creates issue in both repos
```
