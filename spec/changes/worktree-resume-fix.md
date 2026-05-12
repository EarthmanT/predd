# Fix: Worktree Resume After Partial Failure

## Problem

`_worktree_cleanup` runs `git worktree remove --force {path}` before recreating a worktree. This works if the directory exists. If the directory was deleted manually (or never created), git still has the branch registered as a worktree and `git worktree add` fails with exit 128.

`git worktree prune` only removes registrations for directories that no longer exist — but it must run first, before `git worktree remove`.

Current order:
1. `git worktree remove --force {path}` — fails silently if path gone
2. `git worktree prune` — now cleans it up
3. `git branch -D {branch}` — works
4. `git worktree add` — **still fails** if step 1 failed and step 2 didn't catch it in time

## Fix

Reverse the order: prune first, then remove, then delete branch.

```python
def _worktree_cleanup(local_repo: Path, wt_path: Path, branch: str | None = None) -> None:
    subprocess.run(["git", "worktree", "prune"], cwd=local_repo, capture_output=True)
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_path)],
        cwd=local_repo, capture_output=True,
    )
    subprocess.run(["git", "worktree", "prune"], cwd=local_repo, capture_output=True)
    if branch:
        subprocess.run(["git", "branch", "-D", branch], cwd=local_repo, capture_output=True)
```

Prune twice: once before remove (catches deleted directories), once after remove (catches anything remove left behind).

## Also

Log a warning (not error) when `git worktree add` fails due to a stale registration — currently it raises immediately and marks the issue `failed`. Should retry once after cleanup before failing.
