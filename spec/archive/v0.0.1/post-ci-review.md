# Post-CI Review of Hunter-Created PRs → Auto-File Issues

## Problem

Currently the loop runs hunter → predd → human review → merge. Nothing reviews the *outcome* of hunter's work once CI has run. Manually, you used to open every PR, scan the workflow logs, and discover small improvements (flaky tests, missed edge cases, slow steps, warnings the agent ignored). That discovery surface is gone.

We want predd to do that pass automatically: when it sees one of hunter's own PRs with finished CI, run a semantic review of the diff + workflow logs and file new GitHub issues for any improvements worth shipping. Those issues then flow back through the normal hunter pipeline.

## Proposed Behaviour

### Trigger

During each predd poll cycle, after the existing PR-review pass, run a second pass over PRs in state `submitted`. For each, check the three trigger conditions:

1. **Authored by hunter** — at least one of:
   - PR head ref starts with `cfg.branch_prefix` (default `usr/at`), OR
   - PR labels include `sdd-proposal` or `sdd-implementation`, OR
   - PR author login equals `cfg.github_user` (covers PRs hunter pushed)
2. **CI is finished** — all check runs for `headRefOid` are in conclusion `success`, `failure`, `cancelled`, `timed_out`, `action_required`, or `neutral`. Not `queued` or `in_progress`.
3. **Not yet post-CI-reviewed** — predd state entry does not have `post_ci_reviewed = true`.

If all three hold, dispatch to `run_post_ci_review(cfg, state, repo, pr_number)`.

### Module Layout

Add a new file `sentinel.py` (sibling of `predd.py`, `hunter.py`, `obsidian.py`). It imports shared helpers from `predd.py` via the same `importlib` pattern hunter uses. Predd imports it lazily inside the poll loop:

```python
from sentinel import run_post_ci_review
```

`sentinel.py` exposes:

```python
def run_post_ci_review(cfg, state, repo: str, pr_number: int) -> None
```

Plus helpers (private):
- `_fetch_check_runs(repo, sha) -> list[dict]`
- `_fetch_workflow_logs(repo, pr_number) -> str`  (gh run view --log on all completed runs for the PR head)
- `_review_pr_and_logs(cfg, diff: str, logs: str, pr_context: dict) -> list[Finding]`
- `_already_filed(repo, fingerprint, github_user) -> bool`
- `_open_auto_filed_count(repo, github_user) -> int`
- `_file_finding(cfg, repo, source_pr, finding) -> int | None`  (returns new issue #, or None if at cap / duplicate)

### Finding Schema

The LLM analyzer is instructed to return strict JSON. Anything outside this schema is discarded:

```json
{
  "findings": [
    {
      "title": "short, imperative (e.g. 'Add retry to flaky CSV-parse test')",
      "severity": "blocker | concern | nit",
      "source": "code:path/file.py:42 | workflow:run/<run_id>:job_name",
      "rationale": "why this matters in 1-2 sentences",
      "suggested_fix": "concrete change to make"
    }
  ]
}
```

Only `severity == "blocker"` or `"concern"` get filed. `nit` is logged to decisions.jsonl with event `post_ci_finding_skipped` but not turned into an issue.

### Analyzer Prompt (anchor for the skill)

Stored at `~/.windsurf/skills/post-ci-review/SKILL.md`. Path configurable as `cfg.post_ci_skill_path` (default `~/.windsurf/skills/post-ci-review/SKILL.md`). The skill receives:

- The PR diff (`gh pr diff <pr> --repo <repo>`)
- The workflow logs (all jobs, truncated per-job to last 200 lines if longer)
- PR metadata (title, body, linked issue if any)

Skill should be written with a high bar:
- "If you cannot articulate a concrete suggested_fix, do not file the finding."
- "Style preferences are nits. Correctness, flakiness, security, performance regressions are concerns or blockers."
- "Findings about the framework or unrelated code are out of scope — file only findings about the diff or the logs from runs of this diff."

### Filing the Issue

Issue body template:

```markdown
**Source:** PR #<source_pr>
**Severity:** <severity>
**Detected by:** sentinel (post-CI review)

## What

<rationale>

## Where

<source>

## Suggested fix

<suggested_fix>

---
<!-- sentinel-fingerprint: <hash> -->
<!-- sentinel-source-pr: <source_pr> -->
```

Fingerprint is `sha256(source_pr + ":" + title + ":" + source)[:16]`. Before filing, search:

```bash
gh issue list --repo <repo> --state all --search "sentinel-fingerprint: <hash>" --json number
```

If any match, skip. If `cfg.auto_assign_filed_issues` is true (default true), assign to `cfg.github_user` so hunter will pick it up. Apply two labels: `{github_user}:auto-filed` and the severity (`auto-filed:blocker` / `auto-filed:concern`).

### Backpressure

Before filing, count open issues with the `{github_user}:auto-filed` label. If >= `cfg.max_open_auto_issues` (default 5), do not file. Instead emit:

```
log_decision("post_ci_finding_deferred", repo=repo, pr=source_pr,
             title=finding.title, severity=finding.severity, fingerprint=hash)
```

Deferred findings are not retried automatically — they're meant to be re-discovered on the next poll once the cap drains.

### State

Mark the source PR as reviewed by adding `post_ci_reviewed = true` and `post_ci_reviewed_at = <iso>` to its predd state entry. Counters (`post_ci_findings_filed`, `post_ci_findings_deferred`) optional — useful for the status page.

Also emit `log_decision("post_ci_review_completed", repo=repo, pr=source_pr, findings_filed=N, findings_deferred=M)`.

### Config Additions

```toml
# Post-CI review of hunter-created PRs
post_ci_review_enabled = true
post_ci_skill_path = "~/.windsurf/skills/post-ci-review/SKILL.md"
max_open_auto_issues = 5
auto_assign_filed_issues = true
```

Add to `Config.__init__`, `Config.to_dict`, and `DEFAULT_CONFIG_TEMPLATE` in `predd.py`.

## Status Page Integration

Add a small "Sentinel" panel to the dashboard:
- Count of `post_ci_review_completed` events in last 7 days
- Count of currently-open `{github_user}:auto-filed` issues (live from gh, not state)
- Last finding filed (title + link)

## Out of Scope

- Reviewing PRs not created by hunter (e.g. teammates' PRs). Different problem.
- Periodic sweeps of the repo independent of CI (e.g. "review main weekly"). Possible follow-on spec.
- Posting findings as PR comments instead of new issues. The whole point is to feed the hunter pipeline.
- Long-log summarization beyond per-job tail truncation. If logs are huge, fix that separately.

## Acceptance Criteria

1. `predd start --once` against a repo where a hunter PR has finished CI invokes `run_post_ci_review` exactly once and sets `post_ci_reviewed = true`.
2. With `max_open_auto_issues = 0`, no issues are filed; findings are logged with `post_ci_finding_deferred`.
3. With `max_open_auto_issues = 5` and 6 valid findings, 5 issues are filed and 1 is deferred.
4. Filing the same finding twice (across two runs) does not create a duplicate; the second pass detects the fingerprint and skips.
5. Issues filed get labels `{github_user}:auto-filed` and severity label, are assigned to `cfg.github_user`, and carry the fingerprint comment in the body.
6. Unit tests in `test_sentinel.py` cover: trigger condition matching, finding parsing, fingerprint generation, dedup search, backpressure cap, schema validation.
7. Existing tests pass: `uv run --with pytest pytest test_pr_watcher.py test_hunter.py test_obsidian.py test_sentinel.py -q`.

## Files Touched

- `sentinel.py` — new
- `predd.py` — Config additions, poll-loop hook, DEFAULT_CONFIG_TEMPLATE
- `test_sentinel.py` — new
- `CLAUDE.md` — document sentinel daemon and `post_ci_review_enabled` config knob

No changes to `hunter.py` or `obsidian.py`. Hunter consumes the filed issues through its existing assigned-issues poll.
