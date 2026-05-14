# State Reconciliation, Shutdown Cleanup, and Pre-flight Diff Check

Three related improvements that make hunter and predd more resilient to state loss and wasted work.

---

## 1. State Reconciliation (Hunter)

### Problem

If the hunter state file is cleared, manually edited, or simply absent when an issue already has work done on GitHub, hunter silently ignores the issue. The most acute case: an issue with a merged `sdd-proposal` PR and no state entry will never get an implementation started. Hunter only processes assigned issues that are either new or already tracked — it has no path to discover GitHub-side progress that isn't reflected in state.

### Fix

Add `reconcile_assigned_issues(cfg, state, repos)`, called once per poll cycle immediately after `resume_in_flight_issues` and before the main per-repo loop.

For each issue assigned to `@me` across all repos:
- If a state entry exists for the key → skip (normal flow handles it).
- If no state entry → query GitHub for proposal/impl PRs and inject the appropriate state.

**Inference rules (checked in order):**

| GitHub evidence | Injected status | Notes |
|----------------|-----------------|-------|
| Merged `sdd-implementation` PR whose title or body contains `#<issue>` | `submitted` | Work done; nothing to do. |
| Open `sdd-implementation` PR whose title or body contains `#<issue>` | `implementing` | Resume monitoring/review. |
| Merged `sdd-proposal` PR whose title or body contains `#<issue>` | `proposal_open` | Resume will kick off impl next cycle. |
| No proposal PR found | (no injection) | Treat as new; normal pickup handles it. |

When injecting `implementing`, also record `impl_pr` from the PR number found.
When injecting `proposal_open`, also record `proposal_pr` from the PR number found.
Include `issue_number`, `repo`, and `title` in all injected entries so downstream functions can operate on them.

**PR search:** use `gh pr list` with `--label sdd-proposal --state merged` and `--label sdd-implementation --state all`, matching on `#{issue_number}` in title or body — identical to the pattern already used in `gh_find_merged_proposal`.

**Logging:** log each injection at INFO level:
```
Reconciled owner/repo!42: injected status 'proposal_open' (found merged proposal PR #17)
```

**Decision log:** emit a `reconciled` event:
```python
log_decision("reconciled", repo=repo, issue=issue_number, injected_status=status, pr=pr_number)
```

### Implementation

```python
def reconcile_assigned_issues(cfg: Config, state: dict, repos: list[str]) -> None:
    for repo in repos:
        try:
            issues = gh_list_assigned_issues(repo)
        except Exception as e:
            logger.warning("reconcile: failed to list issues for %s: %s", repo, e)
            continue

        for issue in issues:
            issue_number = issue["number"]
            title = issue["title"]
            key = f"{repo}!{issue_number}"

            if key in state:
                continue  # already tracked

            # Search for impl PR first (highest specificity)
            impl_pr = _find_impl_pr(repo, issue_number)
            if impl_pr is not None:
                merged = gh_pr_is_merged(repo, impl_pr)
                injected = "submitted" if merged else "implementing"
                entry = dict(status=injected, repo=repo, issue_number=issue_number,
                             title=title, impl_pr=impl_pr if not merged else None)
                state[key] = entry
                save_hunter_state(state)
                logger.info("Reconciled %s: injected status %r (found %s impl PR #%d)",
                            key, injected, "merged" if merged else "open", impl_pr)
                log_decision("reconciled", repo=repo, issue=issue_number,
                             injected_status=injected, pr=impl_pr)
                continue

            # Search for merged proposal PR
            proposal_pr = gh_find_merged_proposal(repo, issue_number, title)
            if proposal_pr is not None:
                entry = dict(status="proposal_open", repo=repo, issue_number=issue_number,
                             title=title, proposal_pr=proposal_pr)
                state[key] = entry
                save_hunter_state(state)
                logger.info("Reconciled %s: injected status 'proposal_open' "
                            "(found merged proposal PR #%d)", key, proposal_pr)
                log_decision("reconciled", repo=repo, issue=issue_number,
                             injected_status="proposal_open", pr=proposal_pr)
```

Add `_find_impl_pr(repo, issue_number) -> int | None` alongside `gh_find_merged_proposal`. It searches `--label sdd-implementation --state all` and matches on `#{issue_number}` in title or body; returns the PR number of the first match, or `None`.

**Call site** — in the poll loop in `start()`, after `resume_in_flight_issues`:

```python
resume_in_flight_issues(cfg, state)
state = load_hunter_state()
reconcile_assigned_issues(cfg, state, hunter_repos)
state = load_hunter_state()
```

### Acceptance Criteria

- Issue with merged proposal PR and no state entry → `proposal_open` injected; impl starts within 2 poll cycles.
- Issue with open impl PR and no state entry → `implementing` injected; review/merge monitoring continues.
- Issue with merged impl PR and no state entry → `submitted` injected; no further work.
- Issue with nothing → no injection; normal new-issue pickup.
- Existing state entries → untouched by reconciliation.

---

## 2. Graceful Shutdown State Cleanup (Hunter)

### Problem

On graceful shutdown (one Ctrl-C, waiting for the in-flight skill to finish), ephemeral states like `in_progress` and `implementing` are written to the state file and left there. On next startup, `resume_in_flight_issues` attempts to resume them. But the worktree from the previous run may be stale, partially committed, or on a branch that already has a remote PR — leading to confusing failures or duplicate work.

The expected user model: one Ctrl-C means "finish what you're doing, then stop cleanly." The state file after a clean shutdown should reflect that the in-flight work did not complete.

### Fix

After the in-flight subprocess exits (and state is updated normally), and before `release_pid_file()`, scan the state file for ephemeral states and reset them to `failed`:

| Status found | Transition | Reason |
|-------------|------------|--------|
| `in_progress` | → `failed` | Proposal skill was running; did not finish. |
| `implementing` | → `failed` | Impl skill was running; did not finish. |
| All others | unchanged | `proposal_open`, `self_reviewing`, `ready_for_review` are stable wait states — leave them for resume logic. |

`self_reviewing` is explicitly excluded: the review skill may have posted a review to GitHub before the shutdown. Resetting it would cause a re-review on next startup. Leave it for `resume_in_flight_issues` to sort out.

**Where:** add a `_cleanup_ephemeral_states(state)` helper and call it in `_shutdown` after the subprocess has finished (i.e., after `_stop` is set and the main `while` loop exits), inside the `finally` block of `start()`.

```python
def _cleanup_ephemeral_states(state: dict) -> None:
    EPHEMERAL = {"in_progress", "implementing"}
    changed = []
    for key, entry in state.items():
        if entry.get("status") in EPHEMERAL:
            entry["status"] = "failed"
            changed.append(key)
    if changed:
        save_hunter_state(state)
        for key in changed:
            logger.info("Shutdown cleanup: reset %s to 'failed' (was ephemeral)", key)
```

Call site in `start()`:

```python
    finally:
        stop_status_server()
        if not once:
            state = load_hunter_state()
            _cleanup_ephemeral_states(state)
            release_pid_file()
```

### Acceptance Criteria

- Ctrl-C during proposal skill → after skill finishes, `in_progress` → `failed` in state file.
- Ctrl-C during impl skill → after skill finishes, `implementing` → `failed` in state file.
- Next startup → issue treated as `failed`, retries cleanly via `resume_in_flight_issues` retry path.
- `proposal_open` and `self_reviewing` → unchanged by shutdown cleanup.
- `--once` mode → cleanup does not run (state is not persisted between one-shot calls).

---

## 3. Pre-flight Diff Size Check (Predd)

### Problem

The current diff-size gate in `process_pr` runs after `setup_worktree`. This means a full git clone/fetch and worktree creation happen before the check decides to skip. For a monorepo or a PR with thousands of files, this wastes minutes and disk space on work that will be immediately discarded. Additionally, the check is inside the `try` block, so any exception in the size check leaves the worktree on disk in a partially-initialised state.

### Fix

Move the size check to before `setup_worktree`, using the GitHub API instead of `git diff --shortstat`.

**API call:**
```
gh pr view {pr_number} --repo {repo} --json additions,deletions
```
Returns `{"additions": N, "deletions": N}`. Sum them and compare to `cfg.max_pr_diff_lines`.

**Placement:** immediately after state validation (after setting `status = "reviewing"`), before `notify_sound` and before any filesystem I/O.

```python
def process_pr(cfg: Config, state: dict, repo: str, pr: dict) -> None:
    pr_number = pr["number"]
    head_sha = pr["headRefOid"]
    ...
    update_pr_state(state, key, head_sha=head_sha, status="reviewing", first_seen=_now_iso())

    # Pre-flight diff-size check — before any worktree setup
    try:
        result = gh_run(["pr", "view", str(pr_number), "--repo", repo,
                         "--json", "additions,deletions"], check=False)
        if result.returncode == 0:
            counts = json.loads(result.stdout)
            total = counts.get("additions", 0) + counts.get("deletions", 0)
            if total > cfg.max_pr_diff_lines:
                msg = (
                    f"Skipping review — diff is too large ({total:,} lines changed, "
                    f"limit is {cfg.max_pr_diff_lines:,}).\n\n"
                    f"Review this PR manually. To raise the limit: "
                    f"`predd config set max_pr_diff_lines {total + 500}`"
                )
                logger.info("Skipping %s — diff too large (%d lines)", key, total)
                gh_pr_comment(repo, pr_number, msg)
                update_pr_state(state, key, status="rejected", head_sha=head_sha)
                log_decision("pr_skip", repo=repo, pr=pr_number,
                             reason="diff_too_large", lines=total)
                return
    except Exception as e:
        logger.warning("Pre-flight diff check failed for %s: %s — proceeding", key, e)
        # Fail open: if we can't check, attempt the review anyway.

    notify_sound(cfg.sound_new_pr)
    notify_toast("New PR", f"{key} — {title}")
    worktree = setup_worktree(cfg, repo, pr_number, head_sha, head_ref)
    ...
```

Remove the old `git diff --shortstat` block that was inside `setup_worktree`.

**Fail-open policy:** if the GitHub API call fails (permissions issue, rate limit, network), log a warning at WARNING level and proceed with the review. Do not skip PRs due to an API failure.

**Config:** `max_pr_diff_lines` (default `2000`) already exists on `Config`. No change needed. Document it clearly in `CLAUDE.md` config table.

### Acceptance Criteria

- Oversized PR → comment posted, state set to `rejected`, no worktree created, returns before `setup_worktree`.
- Normal PR → proceeds as before; worktree created after the check.
- GitHub API failure during check → warning logged, review proceeds normally.
- `max_pr_diff_lines = 0` → check is skipped entirely (treat 0 as "disabled").

---

## Files Touched

| File | Change |
|------|--------|
| `hunter.py` | Add `reconcile_assigned_issues`, `_find_impl_pr`, `_cleanup_ephemeral_states`; call sites in poll loop and `start()` finally block |
| `predd.py` | Move diff-size check before `setup_worktree`, using `gh pr view --json additions,deletions` |
| `test_hunter.py` | Tests for reconciliation scenarios and shutdown cleanup |
| `test_pr_watcher.py` | Tests for pre-flight diff check: oversized skip, normal pass, API failure |
