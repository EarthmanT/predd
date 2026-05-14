# Fix Resume Marker in PR Bodies

## Problem

`resume_in_flight_issues()` tries to recover in-flight proposals after a crash by calling
`gh_list_prs_with_marker(repo, "hunter:issue-{issue_number}")`. This searches PR bodies for
that exact string. But the PR body written at creation time is just:

```
Proposal for issue #123
```

The marker `hunter:issue-123` is never written into the body, so the search always returns
empty, resume always fails, and every crashed issue rolls back instead of continuing.

Same bug applies to impl PRs: marker `hunter:impl-{issue_number}` is searched but never written.

## Fix

Write the marker into the PR body at creation time, inside an HTML comment so it's invisible
in the GitHub UI.

### Proposal PR body (in `gh_create_branch_and_pr`)

```python
body = f"Proposal for issue #{issue_number}\n\n<!-- hunter:issue-{issue_number} -->"
```

### Impl PR body

```python
body = f"Implementation for issue #{issue_number}\n\n<!-- hunter:issue-{issue_number} -->\n<!-- hunter:impl-{issue_number} -->"
```

Both markers go into the impl body so both search patterns (`hunter:issue-N` and
`hunter:impl-N`) match.

## Scope

Only two call sites need to change — both are in `gh_create_branch_and_pr()` or its callers
that set the PR body string.

## Testing

- Test that proposal PR body contains `hunter:issue-{N}` marker
- Test that impl PR body contains both `hunter:issue-{N}` and `hunter:impl-{N}` markers
- Test that `gh_list_prs_with_marker` finds a PR after the fix
