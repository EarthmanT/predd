# Sprint Gate Configuration

## Problem

The sprint filter used when querying Jira is hardcoded in two places:

1. **Jira API JQL** (in `jira-api-integration.md`): the planned `ingest_jira_api` function uses `sprint in openSprints()` implicitly. There is no config field to change this.
2. **CSV ingest** (`hunter.py`): `ingest_jira_csv` treats Sprint as a hard gate — rows without a sprint value are skipped — but there is no way to disable or change this gate without editing code.

This is too rigid for two common situations:

- **Backlog grooming / triage:** A user wants to let hunter pick up issues that have been assigned but not yet placed in a sprint. With the current behavior, those issues are silently skipped.
- **Named sprint targeting:** A team uses fixed sprint names (e.g., `DAP09A Sprint-10 2026-05-12`) and wants to restrict ingest to exactly one sprint, not "whatever is currently active".

## Solution

Add a `jira_sprint_filter` config field that controls the sprint gate for both the Jira API and CSV ingest paths. The field accepts three values:

| Value | Meaning |
|-------|---------|
| `"active"` | Only issues in the currently active sprint (`openSprints()`). This is the current implicit behavior — the default. |
| `"all"` | No sprint gate. All project issues matching other filters are included. |
| `"named:<sprint-name>"` | Only issues in the named sprint (exact match). |

## Config Schema

New top-level field in `config.toml`:

```toml
# Sprint filter for Jira ingest (API and CSV paths).
# "active" = only issues in the currently active sprint (default)
# "all"    = no sprint gate, include all project issues
# "named:DAP09A Sprint-10 2026-05-12" = only the named sprint
jira_sprint_filter = "active"
```

Examples:

```toml
jira_sprint_filter = "active"                          # default
# jira_sprint_filter = "all"                           # no sprint gate
# jira_sprint_filter = "named:DAP09A Sprint-10 2026-05-12"
```

### Config Class Changes

Add `jira_sprint_filter: str` to `Config.__init__` with default `"active"`.

Validation at load time:

- Accept `"active"`, `"all"`, or any string matching `^named:.+$`.
- Log a warning and fall back to `"active"` for unrecognized values — do not raise.

```python
@dataclass
class Config:
    jira_sprint_filter: str = "active"  # "active" | "all" | "named:<name>"
```

## JQL Construction

The Jira API path builds a JQL string per query. Extract a helper function:

```python
def _sprint_jql_clause(sprint_filter: str) -> str | None:
    """Return the sprint JQL clause for the given filter, or None if no clause needed."""
    if sprint_filter == "active":
        return "sprint in openSprints()"
    elif sprint_filter == "all":
        return None  # no clause — callers omit this fragment
    elif sprint_filter.startswith("named:"):
        name = sprint_filter[len("named:"):]
        escaped = name.replace('"', '\\"')
        return f'sprint = "{escaped}"'
    else:
        # unrecognized — fall back to active (already warned at load time)
        return "sprint in openSprints()"
```

The `ingest_jira_api` function composes its JQL from clauses:

```python
clauses = [f"project = {project_key}", "assignee = currentUser()"]
sprint_clause = _sprint_jql_clause(cfg.jira_sprint_filter)
if sprint_clause:
    clauses.append(sprint_clause)
if cfg.skip_jira_issue_types:
    type_list = ", ".join(f'"{t}"' for t in cfg.skip_jira_issue_types)
    clauses.append(f"issuetype not in ({type_list})")
jql = " AND ".join(clauses)
```

## CSV Ingest Changes

`ingest_jira_csv` currently skips rows where the Sprint column is empty (sprint as a hard gate). The new behavior depends on `jira_sprint_filter`:

| Filter | CSV behavior |
|--------|-------------|
| `"active"` | Skip rows with no sprint value (current behavior). Also skip rows whose sprint value does not contain "active" or is not listed as an active sprint. Because the CSV is a static export, "active sprint" detection uses a configurable `jira_active_sprint_name` field (see below) or a simple heuristic: keep the row if its sprint value is non-empty. |
| `"all"` | Include all rows regardless of sprint value. Sprint column may be empty — that is fine. |
| `"named:<sprint-name>"` | Include only rows where the Sprint column value exactly matches `<sprint-name>`. Skip rows with non-matching or empty sprint values. |

### `jira_active_sprint_name` field (optional)

When `jira_sprint_filter = "active"` and using CSV ingest, there is no live Jira API to ask which sprint is active. Add an optional `jira_active_sprint_name` config field:

```toml
# Used only for CSV ingest with jira_sprint_filter = "active".
# If set, only rows whose Sprint column matches this name are included.
# If not set, any non-empty sprint value is accepted (previous behavior).
# jira_active_sprint_name = "DAP09A Sprint-10 2026-05-12"
```

Default: empty string (falsy). When empty and filter is `"active"`, fall back to the existing behavior (non-empty sprint = accepted).

This field is a lightweight workaround for the inherent staleness of CSV exports. Users who want precise filtering should either use `named:` or set `jira_active_sprint_name`.

### Updated CSV gate logic

Replace the current hard-gate block in `ingest_jira_csv` with:

```python
def _passes_sprint_gate(sprint_value: str, cfg: Config) -> bool:
    f = cfg.jira_sprint_filter
    if f == "all":
        return True
    elif f == "active":
        if not sprint_value:
            return False
        if cfg.jira_active_sprint_name:
            return sprint_value == cfg.jira_active_sprint_name
        return True  # non-empty sprint accepted
    elif f.startswith("named:"):
        target = f[len("named:"):]
        return sprint_value == target
    else:
        # unrecognized — treat as "active"
        return bool(sprint_value)
```

Call this function where the current sprint-gate check lives. If `_passes_sprint_gate` returns `False`, skip the row and log at DEBUG level: `"csv: skip row {key} — sprint gate ({cfg.jira_sprint_filter})"`.

## DEFAULT_CONFIG_TEMPLATE Update

Add the new field to the template written by `load_config()` when no config exists:

```toml
# Sprint filter for Jira ingest.
# Options: "active" (default), "all", "named:<sprint-name>"
jira_sprint_filter = "active"
```

## Testing

- `test_sprint_jql_active`: `_sprint_jql_clause("active")` returns `"sprint in openSprints()"`.
- `test_sprint_jql_all`: `_sprint_jql_clause("all")` returns `None`.
- `test_sprint_jql_named`: `_sprint_jql_clause("named:Sprint-10")` returns `'sprint = "Sprint-10"'`.
- `test_sprint_jql_named_escapes_quotes`: sprint name containing `"` is escaped correctly.
- `test_sprint_jql_unknown_falls_back`: unrecognized value returns `"sprint in openSprints()"`.
- `test_passes_sprint_gate_all`: `_passes_sprint_gate("", cfg_all)` returns `True`; `_passes_sprint_gate("any", cfg_all)` returns `True`.
- `test_passes_sprint_gate_active_empty`: `_passes_sprint_gate("", cfg_active)` returns `False`.
- `test_passes_sprint_gate_active_nonempty`: `_passes_sprint_gate("Sprint-10", cfg_active)` returns `True` when `jira_active_sprint_name` is not set.
- `test_passes_sprint_gate_active_named_match`: `_passes_sprint_gate("Sprint-10", cfg_active_named)` returns `True` when `jira_active_sprint_name = "Sprint-10"`.
- `test_passes_sprint_gate_active_named_mismatch`: returns `False` when sprint value differs.
- `test_passes_sprint_gate_named_filter_match`: `_passes_sprint_gate("Sprint-10", cfg_named_sprint10)` returns `True`.
- `test_passes_sprint_gate_named_filter_mismatch`: `_passes_sprint_gate("Sprint-9", cfg_named_sprint10)` returns `False`.
- `test_csv_ingest_all_includes_no_sprint_rows`: with `jira_sprint_filter = "all"`, rows with empty sprint column are ingested.
- `test_csv_ingest_active_skips_no_sprint_rows`: with `jira_sprint_filter = "active"`, rows with empty sprint column are skipped.
- `test_config_load_default_sprint_filter`: config without `jira_sprint_filter` defaults to `"active"`.
- `test_config_load_invalid_sprint_filter`: unrecognized value logs warning, defaults to `"active"`.

## Out of Scope

- Multi-sprint selection (e.g., `"named:Sprint-9,Sprint-10"`). Single filter value only.
- Dynamically fetching the active sprint name from the Jira API for use in CSV filtering. If you have API access, use `jira_sprint_filter = "active"` with the API path directly.
- Filtering by sprint start/end dates.
- Per-repo sprint filter overrides. `jira_sprint_filter` is global for now. Per-repo overrides can be added to `RepoConfig` in a follow-on spec.

## Files Touched

- `predd.py` — `Config` dataclass: `jira_sprint_filter`, `jira_active_sprint_name`; `_sprint_jql_clause` helper; `DEFAULT_CONFIG_TEMPLATE`
- `hunter.py` — `_passes_sprint_gate` helper; `ingest_jira_csv` sprint gate block; `ingest_jira_api` JQL construction (when that function is implemented per jira-api-integration.md)
- `test_hunter.py` — new sprint gate tests
- `test_pr_watcher.py` — config load/default tests for new fields
