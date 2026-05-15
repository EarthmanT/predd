# CLAUDE.md — predd project context

Reference document for agents working on this codebase. Read this before touching anything.

---

## What this project is

Two background daemons that automate GitHub work:

| Tool | What it does |
|------|--------------|
| **predd** | Polls GitHub for open PRs, reviews them via AI (claude or devin), posts inline comments |
| **hunter** | Polls GitHub for issues assigned to you, writes proposals, implements, self-reviews, closes on merge |
| **obsidian** | (planned) Observes patterns in logs/feedback, generates improvement specs |

The self-improvement loop: `obsidian observe` writes notes → `obsidian analyze` produces specs → hunter picks up specs → proposes → implements → merged → tool improves.

---

## Repository layout

```
predd.py                  # predd daemon + shared base code
hunter.py                 # hunter daemon (imports from predd.py via importlib)
test_pr_watcher.py        # predd tests
test_hunter.py            # hunter tests
scripts/worktree.sh       # worktree management helper
.skills/worktree-management/  # worktree workflow skill
spec/archive/v0.0.1/      # implemented specs (design decisions)
spec/changes/             # pending specs (future work)
README.md
```

Config and state live outside the repo at `~/.config/predd/`.

---

## Worktree Workflow

**NEVER work directly in the main checkout. ALWAYS use worktrees for code changes.**

This repo includes `scripts/worktree.sh` to manage git worktrees:

```bash
# Create a new worktree for a branch
scripts/worktree.sh create <branch-name> [base-branch]

# Checkout a PR into a worktree
scripts/worktree.sh pr <pr-number>

# List all worktrees
scripts/worktree.sh list

# Remove a worktree
scripts/worktree.sh remove <branch-name>
```

Worktrees are created at: `~/windsurf/worktrees/predd/<branch-name>/`

**Why:** Keeps main checkout clean (always on `main`), enables parallel work on multiple branches, prevents accidental commits to wrong branch.

See `.skills/worktree-management/SKILL.md` for detailed workflow.


---

## Architecture decisions

### Single-file scripts (PEP 723)

Both files use `uv` shebang with inline dependencies:
```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["click"]
# ///
```
Run directly as scripts or via symlink. No package install.

### Shared code: predd.py is the base

`hunter.py` imports from `predd.py` at runtime using `importlib`:
```python
_predd_spec = importlib.util.spec_from_file_location("predd", Path(__file__).resolve().parent / "predd.py")
_predd = importlib.util.module_from_spec(_predd_spec)
_predd_spec.loader.exec_module(_predd)
```
Symbols imported: `Config`, `load_config`, `load_state`, `save_state`, `notify_sound`, `notify_toast`, `_run_proc`, `_PWSH`, `_DEVIN_STRIP_ENV`, `repo_slug`, `find_local_repo`, `setup_new_branch_worktree`, `_now_iso`.

**Do not duplicate these in hunter.py.** If you need to change shared behavior, change predd.py.

### Config

Single file: `~/.config/predd/config.toml`, shared by both tools. Loaded by `load_config()` in predd.py. Key fields:

| Field | Default | Notes |
|-------|---------|-------|
| `repos` | (required) | watched by both predd and hunter |
| `predd_only_repos` | `[]` | predd only |
| `hunter_only_repos` | `[]` | hunter only |
| `github_user` | (required) | PRs authored by this user are skipped by predd |
| `worktree_base` | (required) | where git worktrees are created |
| `skill_path` | `~/.windsurf/skills/pr-review/SKILL.md` | |
| `proposal_skill_path` | `~/.windsurf/skills/proposal/SKILL.md` | |
| `impl_skill_path` | `~/.windsurf/skills/impl/SKILL.md` | |
| `backend` | `devin` | `devin` or `claude` |
| `model` | `swe-1.6` | `claude-opus-4-7` when backend=claude |
| `trigger` | `ready` | `ready` (all non-draft) or `requested` (explicit reviewer) |
| `max_review_fix_loops` | `1` | self-review cycles before flagging human |
| `auto_review_draft` | `false` | wait for PR to leave draft before self-reviewing |
| `branch_prefix` | `usr/at` | prefix for hunter-created branches |
| `max_resume_retries` | `2` | retries before rolling back a stuck issue |
| `max_new_issues_per_cycle` | `1` | per-repo cap on new issue pickup per poll |
| `orphan_scan_interval` | `10` | cycles between orphaned-label scans (0 = startup only) |
| `auto_label_prs` | `true` | auto-apply sdd-proposal/sdd-implementation labels |
| `collect_pr_feedback` | `true` | capture PR review feedback in hunter state |

If `config.toml` doesn't exist, `load_config()` writes a default template and exits — prompting the user to fill it in.

### State files

| File | Purpose |
|------|---------|
| `~/.config/predd/state.json` | predd PR state; key = `owner/repo#N` |
| `~/.config/predd/hunter-state.json` | hunter issue state; key = `owner/repo!N` |

State is written atomically: write to `.json.tmp` then `rename()`.

### Decision logs

Structured JSONL, one record per event. Never raises — wrapped in try/except.

| File | Used by |
|------|---------|
| `~/.config/predd/decisions.jsonl` | predd |
| `~/.config/predd/hunter-decisions.jsonl` | hunter |

Each record: `{"ts": "...", "event": "...", ...fields}`. Events include `pr_review_started`, `pr_review_posted`, `pr_review_failed`, `pr_skip`, `issue_pickup`, `proposal_created`, `proposal_merged`, `impl_created`, `issue_closed`, `rollback`, `pr_feedback`, `claim_failed`, `skill_no_commits`.

### Obsidian vault

`~/.config/predd/obsidian/` — planned home for observation notes and analysis output.

---

## Key conventions (hard-won)

### gh_run: elif not if for error classification

`gh_run` uses `elif` to distinguish permanent vs transient errors:
```python
if any(x in stderr for x in _PERMANENT_ERRORS):
    result.check_returncode()  # fail immediately, no retry
elif any(x in stderr for x in _TRANSIENT_ERRORS):
    time.sleep(...)  # retry with backoff
else:
    result.check_returncode()  # unknown — also fail immediately
```
Permanent errors: `not found`, `404`, `401`, `403`, `unauthorized`, `forbidden`, `422`, `unprocessable`, `already exists`. Transient: `rate limit`, `502`, `503`, `504`, `timeout`, `connection`.

### git worktree: double-prune

Always prune before and after `worktree remove` to handle manually-deleted directories:
```python
subprocess.run(["git", "worktree", "prune"], ...)
subprocess.run(["git", "worktree", "remove", "--force", path], ...)
subprocess.run(["git", "worktree", "prune"], ...)  # second prune
```
Without double-prune, `git worktree add` fails with exit 128 when a prior registration refers to a missing directory.

### claude -p: stdin not CLI arg

Pass prompts via stdin, not as CLI argument. Strip `ANTHROPIC_API_KEY` to use OAuth. Always use `--dangerously-skip-permissions`:
```python
env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
["claude", "-p", "--dangerously-skip-permissions", "--model", cfg.model]
# prompt passed as stdin_text
```

### devin: use setsid

```python
["setsid", "devin", "-p", "--permission-mode", "auto", "--model", cfg.model, "--", prompt]
```
Strip `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` from env.

### Skills: task-first prompt structure

The prompt passed to the backend must put the concrete task before the skill instructions:
```python
prompt = f"Run the following workflow for this task:\n\n{arguments}\n\n---\n\n{skill_body}"
```
Reversed order (skill first, task second) causes the model to wait for user input or ignore the task.

Strip YAML frontmatter from skill files before sending.

### subprocess.TimeoutExpired: kill then communicate

```python
except subprocess.TimeoutExpired:
    proc.kill()
    proc.communicate()  # reap the process
    raise
```
Without `communicate()` after `kill()`, the process becomes a zombie.

### PR labels (hunter)

| Label | Meaning |
|-------|---------|
| `sdd-proposal` | PR is a proposal |
| `sdd-implementation` | PR is an implementation |

Hunter finds its own proposal PRs by searching for merged `sdd-proposal` PRs whose title or body references `#<issue_number>`. The old `hunter:issue-N` body marker is not used for discovery.

### Issue labels (hunter)

| Label | Applied when |
|-------|-------------|
| `{github_user}:in-progress` | Issue claimed, proposal work starting |
| `{github_user}:proposal-open` | Proposal PR open, waiting for merge |
| `{github_user}:implementing` | Implementation in progress |

Hunter removes all its labels and closes the issue when the impl PR merges. It does not reopen for verification.

---

## Spec Kit integration (Phase I)

When `speckit_enabled = true`, hunter uses BPA-Specs artifacts instead of the legacy `proposal_skill_path` / `impl_skill_path` for issues that have a matching capability folder.

**Config fields** (all in `predd.py` `Config`):

| Field | Default | Notes |
|-------|---------|-------|
| `speckit_enabled` | `false` | Master switch |
| `speckit_prompt_dir` | `<repo>/prompts/speckit` | Directory with `plan.md` and `implement.md` templates |
| `capability_specs_path` | `None` | Path to BPA-Specs repo's specs folder |
| `speckit_epic_map` | `{}` | `{epic_key: folder_slug}` override for slug mismatches |

**BPA-Specs folder contract**: `<capability_specs_path>/<capability-slug>/` with `constitution.md`, `spec.md`, optional `clarifications.md`, and `stories/<jira-key>/spec.md`.

**Proposal phase**: hunter resolves capability folder from Jira epic, copies artifacts to `spec-refs/` in the branch, then runs `prompts/speckit/plan.md` template via the existing backend. `plan.md` is written to the repo root. `used_speckit: true` is stored in hunter state.

**Impl phase**: after proposal merges, hunter reads `spec-refs/` + `plan.md` from the merged branch and runs `prompts/speckit/implement.md`. `used_speckit` in state controls the fork.

**Fallback**: no capability folder → `log_decision("speckit_no_capability")` → legacy `proposal_skill_path` used, `used_speckit: false`.

**Branch naming**: `spec_branch()` → `{branch_prefix}/{issue_id}-spec-{slug}` (vs `{issue_id}-proposal-{slug}` legacy).

**Key functions** in `hunter.py`: `spec_branch`, `resolve_capability_folder`, `read_bpa_specs_bundle`, `pin_capability_sha`, `copy_spec_refs`, `load_speckit_prompt`, `run_speckit_plan`, `run_speckit_implement`.

---

## Hunter issue state machine

```
new → in_progress → proposal_open → implementing → self_reviewing → ready_for_review → submitted
                                                                  ↘ (loop exhausted)  ↗
failed (retryable via rollback)
awaiting_verification (legacy, kept for compat, treated as terminal)
```

State key format: `owner/repo!<issue_number>`

Transitions:
- `new` → `in_progress`: `process_issue()` — claim label, create worktree, run proposal skill
- `in_progress` → `proposal_open`: proposal PR created
- `proposal_open` → `implementing`: merged `sdd-proposal` PR found for issue
- `implementing` → `self_reviewing`: impl PR exists and not draft (or `auto_review_draft=true`)
- `self_reviewing` → `implementing`: review found issues, ran fix loop
- `self_reviewing` → `ready_for_review`: review approved, or loops exhausted
- `ready_for_review` → `submitted`: impl PR merged, issue closed

Terminal states: `submitted`, `merged` (legacy alias), `awaiting_verification` (legacy), `failed`.

On startup and every `orphan_scan_interval` cycles, `resume_in_flight_issues()` inspects non-terminal entries and either resumes or rolls them back (max `max_resume_retries` attempts before rollback).

---

## predd PR state machine

State key format: `owner/repo#<pr_number>`

| Status | Meaning |
|--------|---------|
| `reviewing` | Skill subprocess running |
| `submitted` | Review posted (or already reviewed by same SHA) |
| `rejected` | Skipped (own PR, already reviewed, etc.) |
| `failed` | Exception during processing |
| `awaiting_approval` | Legacy — draft mode where human approved before posting |

Re-review on new commits: if `head_sha` changes on a `submitted` PR, predd re-reviews it.

---

## Running

```bash
# Install
chmod +x predd.py hunter.py
ln -s $(pwd)/predd.py ~/.local/bin/predd
ln -s $(pwd)/hunter.py ~/.local/bin/hunter

# First run generates config
predd start --once   # creates ~/.config/predd/config.toml, then exits

# Edit config, then run daemons
tmux new -s predd && predd start       # Ctrl-B d to detach
tmux new -s hunter && hunter start

# Logs
tail -f ~/.config/predd/log.txt
tail -f ~/.config/predd/hunter-log.txt

# Status
predd list        # pending reviews
hunter status     # counts by state
hunter list       # full state JSON
```

Graceful shutdown: one `Ctrl-C` waits for current task to finish. Second `Ctrl-C` force-kills subprocess and rolls back in-flight state.

---

## Testing

```bash
# Run tests
uv run --with pytest pytest test_pr_watcher.py test_hunter.py -q

# With coverage
uv run --with pytest --with pytest-cov pytest test_hunter.py --cov=hunter

# Single file
uv run --with pytest pytest test_hunter.py -q
```

Target: 80%+ coverage. Tests use `unittest.mock` — no real GitHub calls.

---

## Spec inventory

### Implemented (spec/archive/v0.0.1/)

| Spec | What it added |
|------|---------------|
| `hunter.md` | Original hunter design |
| `decision-logging.md` | JSONL decision logs in `decisions.jsonl` and `hunter-decisions.jsonl` |
| `fix-claude-tool-use.md` | `--dangerously-skip-permissions` flag + strip `ANTHROPIC_API_KEY` |
| `fix-hunter-empty-prs.md` | Block PR creation if skill produced no commits |
| `gh-run-permanent-error-detection.md` | elif chain for permanent vs transient errors |
| `graceful-shutdown.md` | SIGINT/SIGTERM handling, double-Ctrl-C force quit |
| `hunter-close-issue-on-merge.md` | Close issue on impl PR merge (not reopen for verification) |
| `hunter-jira-csv-ingest.md` | Jira CSV → GitHub issues pipeline |
| `hunter-resume-rollback.md` | Resume/rollback logic for crashed in-flight issues |
| `hunter-skill-issue-context.md` | Pass full issue context to skills, not just issue number |
| `label-unlabelled-prs.md` | Auto-label unlabelled proposal/impl PRs |
| `max-one-issue-per-repo-per-cycle.md` | `max_new_issues_per_cycle` cap |
| `orphaned-label-cleanup.md` | Scan and remove all orphaned hunter labels |
| `pr-feedback-collection.md` | Collect review feedback from proposal/impl PRs |
| `predd-re-review-on-new-commits.md` | Re-review on new head SHA |
| `sdd-label-trigger.md` | `sdd-proposal` label as trigger instead of body marker |
| `skip-already-reviewed.md` | Check GitHub state before queuing review |
| `skip-closed-issues.md` | Stop tracking issues closed manually |
| `trigger-mode.md` | `trigger = "ready"` vs `"requested"` |
| `worktree-resume-fix.md` | Double-prune before worktree add |
| `migrate-to-speckit.md` Phase I | Hunter reads BPA-Specs, runs speckit plan + implement |
| `migrate-to-speckit.md` Phase II | predd analyze+tasks review of proposal PRs; re-plan loop |
| `hunter-intake-capability.md` | `hunter intake-capability` + `intake-stories` commands; spec-kit blocks embedded in GH issues; `run_speckit_plan` reads from issue body |

### Pending (spec/changes/)

| Spec | Summary |
|------|---------|
| `analyze-command.md` | `predd analyze` / `hunter analyze` command to read logs and produce improvement specs |
| `hunter-jira-frontmatter.md` | Add Jira metadata frontmatter block to proposal/impl PR bodies |
| `obsidian-observe.md` | Hourly: read GitHub activity, write one Obsidian note per active PR/issue |
| `obsidian-analyze.md` | Daily: read 7 days of observations, produce analysis note + spec files |
| `obsidian-daemon.md` | Third daemon running observe hourly and analyze daily |
| `teams-approval-gate.md` | Local MCP server routing agent approval requests to Microsoft Teams |

---

## Known gaps

- **hunter-jira-frontmatter.md**: Jira API frontmatter on PRs not implemented. CSV ingestion exists but does not add frontmatter to PR bodies.
- **teams-approval-gate.md**: MCP Teams approval server not implemented.
- **obsidian-*.md**: Fully implemented. `obsidian.py` is a standalone daemon with its own `start` command, wired into `start.sh` as a systemd service. `predd observe` and `predd analyze` also expose the same functionality via the predd CLI.
- **`analyze` command**: not yet implemented in either CLI.
- **Resume logic for `in_progress`**: still searches by `hunter:issue-N` body marker (the old pattern) when checking for an existing proposal PR after a crash. This is a known inconsistency since the primary discovery path now uses `sdd-proposal` label.
- **Self-review fix prompt**: hardcoded string `"Fix the review findings on PR #{impl_pr}. Original issue: #{issue_number}"` — does not pass the actual review output to the fix skill.

---

## File paths reference

```
~/.config/predd/
  config.toml           # shared config
  state.json            # predd PR state
  hunter-state.json     # hunter issue state
  decisions.jsonl       # predd decision log
  hunter-decisions.jsonl  # hunter decision log
  log.txt               # predd rotating log (10MB x3)
  hunter-log.txt        # hunter rotating log (10MB x3)
  pid                   # predd PID file
  hunter-pid            # hunter PID file
  review-prompt.md      # fallback review prompt (created on first run)
  obsidian/             # (planned) vault for observation notes
```
