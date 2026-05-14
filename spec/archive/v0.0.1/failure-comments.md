# Failure Comments

## Status: implemented

## Problem

When hunter fails processing an issue or PR — skill crashes, no commits, push failure, etc. — it logs an error and sets `status=failed` with no visible signal on GitHub. The only way to know something went wrong was to tail a log file.

## Solution

When any failure occurs during issue or PR processing, post a comment on the GitHub issue (and PR if one exists) explaining what went wrong and what to do about it.

## Failure cases and comments

### `skill_no_commits` (proposal or impl)

The AI ran but made no changes. Comment on the issue:

> Hunter ran the `{skill}` skill but the AI produced no commits.
>
> To unblock: add file/directory references, concrete acceptance criteria, node type / API names, a pointer to related code, or constraints to the issue description. Hunter retries automatically on the next cycle.

### Skill crash / unexpected exception (proposal or impl)

Subprocess exited unexpectedly. Comment on the issue with the error and worktree path so the user can inspect or pick up manually.

### Push failure

Commits exist but `git push` failed (branch protection, conflict, auth). Comment on the issue with the branch name, error, and worktree path so the user can push manually.

## Label

On any failure, also apply `{github_user}:hunter-failed` to the issue so it's visible in GitHub issue lists without reading comments.

## Config

Controlled by `comment_on_failures = true` (default). Set to `false` to suppress all failure comments.

## Coverage

| Code path | Covered |
|-----------|---------|
| `process_issue` — proposal `skill_no_commits` | ✅ |
| `process_issue` — proposal crash | ✅ |
| `process_issue` — proposal push failure | ✅ |
| `start_implementation` — impl `skill_no_commits` | ✅ |
| `start_implementation` — impl crash | ✅ |
