# Hunter: Jira CSV Ingestion and GitHub Issue Creation

## Problem

Jira stories exist in CSV exports but have no corresponding GitHub issues. Hunter currently only picks up issues that already exist on GitHub and are assigned to the user. This requires a manual step to create GH issues from Jira before hunter can do anything.

## Proposed Behaviour

Hunter polls a configurable `jira_csv_dir` directory for CSV files. For each Jira story in the CSV, if no matching GitHub issue exists, hunter creates one and assigns it to `github_user`. The existing workflow then picks it up on the next poll cycle.

## CSV Format

Hunter expects CSVs exported from Jira. Minimum required columns:

| Column | Notes |
|--------|-------|
| `Issue Key` | e.g. `DAP09A-1184` |
| `Summary` | Issue title |
| `Issue Type` | e.g. Story, Bug, Task |
| `Status` | e.g. Open, In Progress |
| `Epic Link` or `Epic Name` | Epic key or name |
| `Sprint` | e.g. `DAP09A Sprint-10 2026-05-12` |
| `Description` | Full description, may contain `capability: <id> <name>` |

Column names are matched case-insensitively. Missing optional columns are silently skipped.

## GitHub Issue Format

Hunter creates the GH issue with:

**Title:** `[{jira_key}] {summary}`

**Body:**
```markdown
| Field | Value |
|-------|-------|
| Jira | [DAP09A-1184](https://jira.cec.lab.emc.com/browse/DAP09A-1184) |
| Type | Story |
| Epic | [DAP09A-1000](https://jira.cec.lab.emc.com/browse/DAP09A-1000) |
| Sprint | DAP09A Sprint-10 2026-05-12 |
| Capability | 12345 — cool feature |

---

{jira description}
```

Rows with missing values are omitted from the table.

## Duplicate Detection

Hunter checks for an existing GH issue by searching for the Jira key in open issue titles:

```bash
gh issue list --repo {repo} --state open --search "[{jira_key}]" --json number,title
```

If a match is found, hunter skips creation. If the issue exists but is closed, hunter logs a warning and skips.

## Conformance Check

Before creating the issue, hunter checks for required fields. If any are missing:
- Creates the GH issue anyway (so it's visible)
- Adds label `{github_user}:needs-jira-info`
- Does NOT assign it to `github_user` (so hunter won't pick it up for proposal/impl)
- Body includes a warning block listing what's missing

Required for conformance:
- Epic set
- Sprint set
- `capability:` line present in description

## Config

```toml
# Directory to watch for Jira CSV exports
jira_csv_dir = "./jira"

# Base URL for Jira hyperlinks
jira_base_url = "https://jira.cec.lab.emc.com"

# If true, non-conformant issues are created but not picked up
require_jira_conformance = true
```

## Poll Behaviour

CSV ingestion runs at the top of each hunter poll cycle, before issue pickup. CSVs are processed in filename order. All rows in all CSVs are checked every cycle — creation is idempotent (duplicate detection prevents double-creation).

## Implementation Notes

- Use Python's `csv.DictReader` with case-insensitive column matching.
- Jira key parsed from `Issue Key` column directly — no regex needed.
- Capability parsed from `Description` column with regex `capability:\s*(\d+)\s+(.+)`.
- GH issue created via `gh issue create --repo {repo} --title "{title}" --body "{body}" --assignee {github_user}`.
- Only assign to `github_user` if conformant — this is the trigger for the existing workflow.
