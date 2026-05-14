# Jira Conformance: Fix Epic Discovery, Make Sprint a Hard Gate

## Problem

The Jira CSV ingest conformance check has two issues observed in production:

1. **Epic flagged as missing on issues that have an epic in Jira.** `_build_issue_body` (`hunter.py:1197`) looks at exactly two columns: `epic link` and `epic name`. Real-world Jira CSV exports use a variety of column names depending on Jira version, server vs. cloud, and which fields are checked at export time. Adam has verified that all non-Sub-task stories in his Jira project are assigned to an Epic, so when the CSV ingest reports `Epic not set`, it's a parser miss, not real missing data.

2. **Sprint being missing produces a labeled-but-not-assigned issue.** Today, a row missing Sprint creates a non-conformant GitHub issue with the `needs-jira-info` label and no assignee. The desired behavior: if Sprint is missing, do not create the issue at all. Sprint is the load-bearing field — if it isn't there, the work isn't scheduled and shouldn't be on the GitHub board.

Capability is unchanged. Missing capability stays a soft-gate (creates issue, flags non-conformant). User can backfill capability later by editing the Jira description.

## Proposed Behaviour

### 1. Expand Epic column lookup

Update Epic discovery in `_build_issue_body` to check the common Jira CSV column names in priority order. First non-empty value wins:

```python
EPIC_COLUMNS = (
    "epic link",
    "epic name",
    "custom field (epic link)",
    "custom field (epic name)",
    "parent",
    "parent key",
)

def _find_epic(row: dict) -> str:
    for col in EPIC_COLUMNS:
        val = row.get(col, "")
        if val:
            return val
    return ""
```

Replace `epic = row.get("epic link", "") or row.get("epic name", "")` with `epic = _find_epic(row)`.

### 2. Sprint becomes a hard gate

In `ingest_jira_csv` (`hunter.py:1215`), after the `jira_key` / `summary` empty-check and after the existing Sub-task skip from the previous spec, add a Sprint check that skips the row entirely:

```python
sprint = row.get("sprint", "").strip()
if not sprint:
    log_decision(
        "csv_issue_skip",
        repo=repo,
        jira_key=jira_key,
        reason="no_sprint",
    )
    logger.info(
        "CSV ingest: skipping %s — no sprint assigned",
        jira_key,
    )
    continue
```

Same column-lookup-expansion treatment for Sprint, in case Jira exports vary here too:

```python
SPRINT_COLUMNS = (
    "sprint",
    "custom field (sprint)",
)

def _find_sprint(row: dict) -> str:
    for col in SPRINT_COLUMNS:
        val = row.get(col, "")
        if val:
            return val
    return ""
```

Use `sprint = _find_sprint(row).strip()` for the gate check, and remove `"Sprint not set"` from the conformance-missing list in `_build_issue_body` (no longer reachable — if sprint is missing the row never reaches body building).

### 3. One-shot debug log of CSV columns

When `ingest_jira_csv` opens the first CSV file in a poll cycle, log the actual column names at INFO level so we can see what the export contains:

```python
if rows:
    logger.info(
        "CSV ingest: %s columns: %s",
        csv_file.name,
        sorted(rows[0].keys()),
    )
```

This runs once per file per poll. Cheap, and tells us immediately if Epic is genuinely missing vs. living under a column name we don't look at. Drop after a few weeks of stable operation.

### 4. Keep the conformance warning for Epic and Capability

The `> ⚠️ Missing required fields` block in `_build_issue_body` still applies for:
- Epic not set (still soft-gate — issue created, labeled `needs-jira-info`, not assigned)
- No `capability: <id> <name>` line in description (unchanged)

Sprint no longer appears in this list because the row never gets to body-building.

## Out of Scope

- Retroactively closing the non-conformant GitHub issues already created. Manual cleanup via `gh issue close` or filter by `needs-jira-info` label.
- Configurable column aliases via TOML. The expanded lookup lists are hardcoded for now. If a user has a custom Jira field name that isn't covered, they can either rename their export columns or this becomes a follow-on spec.
- Auto-fetching Epic / Sprint from the Jira REST API when missing from CSV. That's `hunter-jira-frontmatter.md` territory; out of scope here.
- Sprint format validation (e.g. "Sprint 23" vs "DAP09A Sprint-10 2026-05-12"). Any non-empty Sprint value is treated as present.

## Acceptance Criteria

1. A CSV row with `Epic Link = "DAP09A-1000"` produces a GitHub issue with the Epic field populated as `[DAP09A-1000](...)`.
2. A CSV row where Epic is missing in `Epic Link` and `Epic Name` but present in `Parent` produces a GitHub issue with the Epic field populated from `Parent`.
3. A CSV row with no Sprint in any of the SPRINT_COLUMNS is NOT created as a GitHub issue. A `csv_issue_skip` decision event with `reason="no_sprint"` is logged.
4. A CSV row with Sprint set but Epic missing creates a GitHub issue with the `needs-jira-info` label (existing soft-gate behavior).
5. A CSV row with Sprint set but no capability line creates a GitHub issue with the `needs-jira-info` label (unchanged).
6. The first CSV file scanned per poll logs a one-line INFO message listing its column names.
7. Unit tests in `test_hunter.py`:
   - `test_find_epic_uses_epic_link`
   - `test_find_epic_falls_back_to_parent`
   - `test_find_epic_returns_empty_when_all_missing`
   - `test_csv_ingest_skips_row_without_sprint`
   - `test_csv_ingest_creates_row_with_sprint`
8. Existing tests pass.

## Files Touched

- `hunter.py` — `EPIC_COLUMNS`, `SPRINT_COLUMNS`, `_find_epic`, `_find_sprint`, `_build_issue_body` Epic lookup, `ingest_jira_csv` Sprint gate + column logging
- `test_hunter.py` — five new tests
