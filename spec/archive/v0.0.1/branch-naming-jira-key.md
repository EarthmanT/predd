# Use Jira Key in Branch Names When Available

## Problem

Hunter currently names branches using the GitHub issue number: `usr/at/377-proposal-<slug>` and `usr/at/377-impl-<slug>`. For issues that originated from Jira (CSV-ingested with titles like `[DAP09A-1841] Summary`), the Jira key is the meaningful identifier — it's what reviewers, the Jira board, and the broader team correlate against. The GitHub issue number is incidental.

Two consequences today:

1. Branch names don't surface the Jira key, so PR titles, branch listings, and `gh pr list` output force a manual lookup back to the issue to find the Jira reference.
2. The branch name and the Jira key drift — a reviewer looking at branch `usr/at/377-proposal-foo` has no fast way to know which Jira ticket it corresponds to.

## Proposed Behaviour

When hunter creates a new proposal or impl branch:

- If the issue title contains a Jira key (parsed by the existing `extract_jira_key` helper, regex `\[([A-Z][A-Z0-9]+-\d+)\]`), use the Jira key: `usr/at/DAP09A-1841-proposal-<slug>`, `usr/at/DAP09A-1841-impl-<slug>`.
- Otherwise, fall back to the GitHub issue number as today: `usr/at/377-proposal-<slug>`.

The change is in the branch-naming functions only. Nothing else in hunter's tracking, claiming, resume, merge-detection, or state machine is touched.

## Backward Compatibility (Hard Requirement)

Existing in-flight work must continue to function unchanged. Specifically:

- Existing `hunter-state.json` entries store the branch name they were created with under `proposal_branch` and `impl_branch`. Those fields are the source of truth for in-flight issues — hunter reads them, not the result of `proposal_branch()`/`impl_branch()`. So old entries keep using old branch names.
- Existing worktrees on disk under `cfg.worktree_base` keep their old directory names. New worktrees use the new naming. They coexist.
- Existing PRs already open on GitHub with old branch names are unaffected. `gh_find_merged_proposal` searches by issue number reference in the PR body/title, not by branch name pattern — works for both.
- `resume_in_flight_issues()` (`hunter.py:1378`) uses `f"hunter:issue-{issue_number}"` as a PR-body marker, not a branch name. Leave it alone.

The branch-naming functions are only called when *creating* a new branch (in `process_issue` and `check_proposal_merged`). They are not called during resume, merge-detection, or any tracking path. So the change has a clean blast radius: future issues only.

## Implementation

### New helper

Add near `extract_jira_key` in `hunter.py` (around line 180):

```python
def issue_identifier(issue_number: int, title: str) -> str:
    """Return Jira key from title if present, else GitHub issue number as string."""
    return extract_jira_key(title) or str(issue_number)
```

### Update `proposal_branch` and `impl_branch`

`hunter.py:626` and `hunter.py:630`:

```python
def proposal_branch(cfg: Config, issue_number: int, title: str) -> str:
    return f"{cfg.branch_prefix}/{issue_identifier(issue_number, title)}-proposal-{issue_slug(title)}"

def impl_branch(cfg: Config, issue_number: int, title: str) -> str:
    return f"{cfg.branch_prefix}/{issue_identifier(issue_number, title)}-impl-{issue_slug(title)}"
```

No other code changes required.

## Out of Scope

- Renaming existing branches on GitHub or in worktrees. Hard no — would invalidate every open PR and require state migration.
- Changing the `hunter:issue-{N}` body marker in `resume_in_flight_issues`. That marker is legacy (CLAUDE.md flags it as such) and a separate concern; if we change it, every old in-flight issue loses its resume path.
- Changing `gh_find_merged_proposal` — already uses issue-number reference in PR body, which is correct and doesn't need to know about branch names.
- Per-repo control over whether to use Jira key. Global behavior. If a repo never gets Jira-keyed issues, the fallback kicks in for every issue and behavior is identical to today.

## Acceptance Criteria

1. A new issue with title `[DAP09A-1900] Add foo` triggers a proposal branch named `usr/at/DAP09A-1900-proposal-add-foo` (or close — exact slug per existing `issue_slug` rules). Verified by unit test, not by live run.
2. A new issue with title `Add foo` (no Jira key) triggers a proposal branch named `usr/at/<gh-issue-number>-proposal-add-foo`. Verified by unit test.
3. An existing state entry created before this change with `proposal_branch = "usr/at/377-proposal-foo"` advances correctly through `check_proposal_merged` → creates an impl branch using the *new* naming for the impl side. The proposal side keeps its old name. (This is the expected mixed-state outcome and is correct.)
4. The same applies to `impl_branch`: new impl branches use the new naming; existing impl branches in state continue to be tracked by their stored name.
5. `resume_in_flight_issues` is unchanged and unit-test coverage for it still passes.
6. New unit tests in `test_hunter.py`:
   - `test_issue_identifier_returns_jira_key_when_present`
   - `test_issue_identifier_falls_back_to_issue_number`
   - `test_proposal_branch_uses_jira_key`
   - `test_proposal_branch_falls_back_to_issue_number`
   - `test_impl_branch_uses_jira_key`
   - `test_impl_branch_falls_back_to_issue_number`
7. Existing tests pass: `uv run --with pytest pytest test_pr_watcher.py test_hunter.py test_obsidian.py -q`.

## Files Touched

- `hunter.py` — new `issue_identifier` helper, two function bodies updated
- `test_hunter.py` — six new tests
- No other files. CLAUDE.md will be updated in a separate documentation pass.
