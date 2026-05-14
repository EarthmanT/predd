# Skip Jira Sub-tasks During CSV Ingest

## Problem

`ingest_jira_csv` (`hunter.py:1215`) creates a GitHub issue for every row in a Jira CSV export, regardless of `Issue Type`. Jira CSVs commonly contain Sub-task rows — children of a parent Story/Bug/Task. These should not become independent GitHub issues, because:

1. The parent issue is the unit of work hunter is meant to track.
2. Sub-tasks roll up under their parent in Jira; treating them as standalone in GitHub fragments the work and inflates the issue count.
3. Hunter would then attempt to propose/implement each sub-task independently, which produces uncoordinated PRs.

Observed: a recent CSV ingest in `fusion-e/ai-bp-toolkit` created issues for multiple Sub-task rows alongside their parents.

## Proposed Behaviour

During CSV ingest, skip rows whose `Issue Type` matches a configured skip list. Default skip list: Sub-task variants (case-insensitive).

A skipped row is logged via `log_decision("csv_issue_skip", repo=repo, jira_key=jira_key, reason="excluded_type", issue_type=<type>)` and otherwise ignored — no GH issue created, no label applied, no error raised.

## Config

Add to `Config.__init__` in `predd.py`:

```python
self.skip_jira_issue_types: list[str] = data.get(
    "skip_jira_issue_types",
    ["sub-task", "subtask", "sub task"],
)
```

Add to `Config.to_dict()`:

```python
"skip_jira_issue_types": self.skip_jira_issue_types,
```

Add to `DEFAULT_CONFIG_TEMPLATE` in `predd.py`:

```toml
# Jira issue types to skip during CSV ingest (case-insensitive)
skip_jira_issue_types = ["sub-task", "subtask", "sub task"]
```

The comparison is case-insensitive: normalize both the config value and the row's `Issue Type` to lowercase before comparing.

## Implementation

In `ingest_jira_csv` (`hunter.py:1215`), after the `jira_key` / `summary` empty-check and before the `title` line:

```python
issue_type = row.get("issue type", "").strip().lower()
if issue_type in {t.lower() for t in cfg.skip_jira_issue_types}:
    log_decision(
        "csv_issue_skip",
        repo=repo,
        jira_key=jira_key,
        reason="excluded_type",
        issue_type=issue_type,
    )
    logger.info(
        "CSV ingest: skipping %s (type=%s) per skip_jira_issue_types",
        jira_key, issue_type,
    )
    continue
```

Important: this check happens *before* the per-repo loop (`for repo in repos`), so a skipped Jira key is skipped for all repos at once. Don't move it inside the loop — that would log the skip N times for N watched repos.

## Out of Scope

- Adding Epic to the default skip list. Epics in CSV exports are rare and contextually useful when present; users can add `"epic"` to the config if they want them skipped. Not a default.
- Hierarchical linking (Sub-task → parent issue body reference). Worth doing later — possibly as a `hunter-jira-frontmatter.md` extension — but separate concern.
- Re-ingesting Sub-task rows as comments on their parent. Same — separate concern, only worth doing once the basic loop is stable.
- Retroactively closing the Sub-task GitHub issues already created. Manual cleanup; the spec only affects future ingests.

## Acceptance Criteria

1. A CSV row with `Issue Type = "Sub-task"` does not create a GitHub issue.
2. A CSV row with `Issue Type = "subtask"` (no hyphen, lowercase) also does not create a GitHub issue.
3. A CSV row with `Issue Type = "Story"` continues to create a GitHub issue.
4. A CSV row with `Issue Type = "Epic"` continues to create a GitHub issue (Epic is not in the default skip list).
5. Each skip emits a `csv_issue_skip` decision event with `reason="excluded_type"` and the issue_type field populated.
6. With a user-provided config `skip_jira_issue_types = ["sub-task", "epic"]`, both Sub-task and Epic rows are skipped, while Story is still created.
7. Unit tests in `test_hunter.py`:
   - `test_csv_ingest_skips_subtask`
   - `test_csv_ingest_skips_subtask_case_insensitive`
   - `test_csv_ingest_creates_story` (positive control)
   - `test_csv_ingest_respects_custom_skip_list`
8. Existing tests pass: `uv run --with pytest pytest test_pr_watcher.py test_hunter.py test_obsidian.py -q`.

## Files Touched

- `predd.py` — Config field, to_dict, DEFAULT_CONFIG_TEMPLATE
- `hunter.py` — skip check in `ingest_jira_csv`
- `test_hunter.py` — four new tests
