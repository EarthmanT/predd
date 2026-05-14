# Label Issues with Jira Key

## Problem

Hunter already extracts Jira keys from issue titles (the `[DAP09A-1184]` pattern, see `hunter.py:1242` and the existing `jira_key` regex in `ingest_jira_csv`). But nothing labels these issues on GitHub, so you can't filter them in the UI or in `gh issue list --label jira`.

## Proposed Behaviour

Apply a `jira` label to any GitHub issue whose title contains a Jira key.

### Where Labels Are Applied

Three places:

1. **CSV ingest path** (`hunter.py:1215`, `ingest_jira_csv`). After `gh_issue_create` succeeds, call `gh_issue_add_label(repo, issue_number, "jira")`. The Jira key is already known here.

2. **Issue pickup path** (`hunter.py:667`, `process_issue`). Before `try_claim_issue`, if the issue title matches `\[([A-Z]+-\d+)\]`, ensure the label is applied. Idempotent. Covers issues created by humans (not through CSV ingest) that have Jira keys in their titles.

3. **One-shot sweep on daemon start.** In `hunter start`'s startup block (before the main poll loop), iterate open issues in `cfg.repos + cfg.hunter_only_repos`, regex-match the title, and label any matches that aren't already labeled. Caps at 100 issues per repo per startup to avoid pathological scans.

### Label Definition

Use the existing helper:

```python
gh_ensure_label_exists(repo, "jira", color="0052CC")  # Jira blue
gh_issue_add_label(repo, issue_number, "jira")
```

Color `0052CC` is the standard Jira brand blue — makes the label recognizable on the GitHub issues list without a custom convention.

### Detection Regex

```python
JIRA_KEY_RE = re.compile(r"\[([A-Z][A-Z0-9]+-\d+)\]")
```

This matches the existing convention. `[A-Z][A-Z0-9]+` allows single-letter project prefixes followed by alphanumerics (`DAP09A-1184`, `BPA-42`, `AI2-7`). The bracketed-and-title-first format is what `ingest_jira_csv` produces and what users typically paste.

A helper in `hunter.py`:

```python
def extract_jira_key(title: str) -> str | None:
    m = JIRA_KEY_RE.search(title or "")
    return m.group(1) if m else None
```

Used by all three apply sites.

### Decision Log Events

```python
log_decision("jira_label_applied", repo=repo, issue=issue_number, jira_key=key)
```

Emitted once per apply. Useful for spotting the sweep's churn on first daemon start.

### Config

No new config keys. The feature is on by default; if anyone wants to disable it, that can be added later.

## Out of Scope

- Hyperlinking the Jira key in the issue body — `ingest_jira_csv` already does this via `_build_issue_body`. For human-created issues, the title is left untouched.
- Labels for type / epic / sprint — those are conformance signals, handled separately by `needs-jira-info`.
- Removing the label if the title is edited to no longer contain the key.
- Parsing Jira keys out of issue bodies. Title-only.

## Acceptance Criteria

1. After `hunter start --once` against a repo with an existing human-created issue titled `[BPA-99] do the thing`, the issue carries the `jira` label.
2. The `jira` label is created in the repo on first use with color `0052CC`.
3. CSV-ingested issues are labeled at creation time (single pass, no extra poll cycle needed).
4. Calling `extract_jira_key` on a title with no key returns `None`; on a title with a key returns the key string.
5. Running `hunter start --once` twice does not duplicate `jira_label_applied` decisions for issues already labeled (the label-add is gated by checking the issue's current labels first).
6. Existing tests pass; new tests in `test_hunter.py` cover `extract_jira_key`, the regex edge cases (multi-letter prefixes, numeric-suffix prefixes, no-match), and the dedup skip on already-labeled issues.

## Files Touched

- `hunter.py` — `extract_jira_key`, `JIRA_KEY_RE`, label calls in three sites, startup sweep helper
- `test_hunter.py` — new tests
