# Migrate Hunter/Predd to Spec Kit Workflow

## Problem

Hunter runs bespoke `proposal_skill` / `impl_skill` prompts with no awareness of upstream spec artifacts. The skills encode all structure themselves. There is no shared vocabulary with the SDD ecosystem, no traceability from business requirements to implementation, and no standard artifact layout reviewers can rely on.

## Architecture

### Upstream Repos (read-only from hunter/predd's perspective)

Two repos feed the workflow:

- **Product Management repo** — capability definitions; source of `constitution.md` content
- **BPA-Specs repo** — owns `specify` + `clarify` output: business reqs, acceptance criteria, eng reqs, cross-team decisions

Spec-kit phases 1–3 (constitution, specify, clarify) happen upstream and are committed to BPA-Specs by humans. Hunter **consumes** them; it does not generate them.

### BPA-Specs Folder Contract

```
<capability_specs_path>/        # e.g. ~/windsurf/projects/bpa-specs/specs
  <capability-slug>/
    constitution.md             # regenerated from PM content at intake
    spec.md                     # capability-level spec
    clarifications.md           # architect/QA/cross-team decisions (optional)
    stories/
      <jira-key>/               # e.g. DAP09A-1234
        spec.md                 # story-level spec (references capability spec)
```

### Phase Mapping

| Spec-kit phase | Owner | Implementation phase |
|---|---|---|
| 1 constitution | PM repo (humans) | — |
| 2 specify | BPA-Specs (humans) | — |
| 3 clarify | BPA-Specs (humans) | — |
| 4 plan | hunter proposal stage | Phase I |
| 5 analyze | predd review of proposal PR | Phase II |
| 6 tasks | predd review of proposal PR | Phase II |
| 7 implement | hunter impl stage | Phase I |

Preserves the 2-PR gate model. Phase I is shippable independently — predd does its existing generic review of the proposal PR unchanged until Phase II lands.

---

## Phase I — Hunter reads BPA-Specs, runs plan + implement

### Identity Resolution

No frontmatter parsing — all identity comes from Jira:

- **Story ID** = Jira ticket key (e.g. `DAP09A-1234`), already on `entry["jira_key"]`
- **Capability** = Jira epic link field → slugified → matched to folder under `capability_specs_path`
- **Epic field**: existing Jira integration fetches multiple epic fields; reuse whichever has a value (same pattern already in `fetch_jira_frontmatter`)
- **Stories without a capability** (Security, Tech Debt, etc.) → epic absent or no matching folder → log `speckit_no_capability` → fall back to legacy `proposal_skill_path`, no error

**Epic → folder mapping**: slugify epic name (lowercase, hyphens). If no folder match, check `speckit_epic_map` config dict. If still no match → legacy fallback.

### Config Additions (`predd.py`)

```python
self.speckit_enabled: bool = data.get("speckit_enabled", False)
self.speckit_prompt_dir: Path = Path(data.get("speckit_prompt_dir",
    str(Path(__file__).parent / "prompts" / "speckit"))).expanduser()
self.capability_specs_path: Path | None = (
    Path(data["capability_specs_path"]).expanduser() if "capability_specs_path" in data else None
)
self.speckit_epic_map: dict[str, str] = data.get("speckit_epic_map", {})
```

Keep `proposal_skill_path` / `impl_skill_path` — legacy fallback path unchanged.

### Artifact Copy (spec-refs/)

At proposal time, hunter copies the relevant BPA-Specs artifacts into the proposal branch:

```
spec-refs/
  constitution.md
  capability-spec.md
  story-spec.md
  clarifications.md      # if present
```

`plan.md` frontmatter records `capability_specs_sha` (HEAD SHA of `capability_specs_path` at proposal time), `capability`, `story_id`. Rationale: self-contained PR review, audit trail, no cross-repo lookup required for reviewers.

Implementation stage reads `spec-refs/` from the merged proposal branch — never re-resolves BPA-Specs at implement time.

### Missing Artifacts

- `story spec.md`, `capability spec.md`, or `constitution.md` missing → **hard fail**: comment on issue, add `{github_user}:speckit-missing-spec` label, skip
- `clarifications.md` missing → **soft warn**: log decision, proceed without it

### New Functions in `hunter.py`

```python
def resolve_capability_folder(cfg, epic_name, epic_key) -> Path | None
```
- `capability_specs_path` not set → None
- Slugify epic_name → check folder exists → return path
- Check `speckit_epic_map[epic_key]` → return path
- Else → log `speckit_no_capability` → None (triggers legacy path)

```python
def read_bpa_specs_bundle(capability_dir, story_id) -> dict
```
- Returns `{"constitution": Path, "capability_spec": Path, "story_spec": Path, "clarifications": Path|None}`
- Hard fail (RuntimeError) if required files missing; soft warn for clarifications

```python
def pin_capability_sha(cfg) -> str
```
- `git -C cfg.capability_specs_path rev-parse HEAD`

```python
def copy_spec_refs(bundle, worktree) -> Path
```
- Writes `spec-refs/` into worktree; returns `worktree / "spec-refs"`

```python
def load_speckit_prompt(cfg, name, **kwargs) -> str
```
- Reads `cfg.speckit_prompt_dir / f"{name}.md"`, calls `.format(**kwargs)`
- Raises `FileNotFoundError` if template missing

```python
def run_speckit_plan(cfg, entry, worktree, issue_number, title, issue_body) -> bool
```
1. Get `jira_key` + epic fields from `entry`; call `resolve_capability_folder` → if None, return False
2. `read_bpa_specs_bundle` → bundle
3. `pin_capability_sha` → sha; store in state as `capability_specs_sha`
4. `copy_spec_refs(bundle, worktree)` and commit
5. Load + render `plan.md` prompt; write to temp file; call `run_skill(cfg, tmp, context, worktree)`
6. Verify `plan.md` written; log `plan_completed`; return True

```python
def run_speckit_implement(cfg, entry, worktree, issue_number, title, issue_body) -> None
```
1. Load `implement.md` prompt with paths from `spec-refs/` + `plan.md` + `tasks.md` in worktree
2. Write to temp file; call `run_skill(cfg, tmp, context, worktree)`

### Fork `process_issue()` (~line 878)

```python
used_speckit = cfg.speckit_enabled and run_speckit_plan(cfg, entry, worktree, ...)
if not used_speckit:
    run_skill(cfg, cfg.proposal_skill_path, context, worktree)
```

Store `used_speckit: bool` in state entry. Branch name: `spec_branch()` helper (`{branch_prefix}/{issue_id}-spec-{slug}`) when speckit; existing `proposal_branch()` when legacy.

### Fork `check_proposal_merged()` (~line 1138)

```python
if entry.get("used_speckit"):
    run_speckit_implement(cfg, entry, worktree, ...)
else:
    run_skill(cfg, cfg.impl_skill_path, context, worktree)
```

### Prompt Templates (`prompts/speckit/`)

Two new files, Python `str.format` placeholders only (no new deps):

- `plan.md` — given spec bundle paths + issue context, produce `plan.md`
- `implement.md` — given `spec-refs/` + `plan.md` + `tasks.md`, execute all tasks

Placeholders: `{issue_number}`, `{issue_title}`, `{issue_body}`, `{constitution_path}`, `{capability_spec_path}`, `{story_spec_path}`, `{clarifications_path}`, `{spec_refs_dir}`, `{plan_path}`, `{tasks_path}`.

---

## Phase II — Predd analyze + tasks + re-plan loop

Depends on Phase I being merged and stable.

### Additional Config (`predd.py`)

```python
self.speckit_run_analyze: bool = data.get("speckit_run_analyze", True)
self.max_analyze_fix_loops: int = data.get("max_analyze_fix_loops", 2)
```

### Additional Prompt Templates

- `analyze.md` — given `spec-refs/` + `plan.md`, output structured `APPROVE` or `INCONSISTENT: <findings>`
- `tasks.md` — given approved `plan.md`, produce `tasks.md`

### `run_speckit_review()` in `predd.py`

Called from `process_pr()` when `cfg.speckit_enabled` and PR has label `sdd-proposal`:

1. Read `spec-refs/` + `plan.md` from worktree
2. Load + run `analyze.md` prompt via existing backend dispatch
3. Parse verdict: first line `APPROVE` or `INCONSISTENT`
4. If `INCONSISTENT`:
   - Post `REQUEST_CHANGES` review with findings body
   - Add `{github_user}:needs-replan` label to source issue (parse issue number from PR body)
5. If `APPROVE`:
   - Load + run `tasks.md` prompt
   - Verify `tasks.md` written; `git commit` + push to proposal branch
   - Post `APPROVE` review with summary

### Re-plan Loop in `hunter.py`

In `proposal_open` poll branch:
- If issue has `needs-replan` label AND `analyze_fix_loops < max_analyze_fix_loops`:
  - Remove label; close proposal PR; delete worktree
  - Increment `analyze_fix_loops`; reset status to `new` for reprocessing
- If loops exhausted: mark `failed`, post escalation comment

Add `analyze_fix_loops: int` (default 0) to state entry.

### Label Updates

Add to `_clean_hunter_labels()`: `{github_user}:needs-replan`, `{github_user}:speckit-missing-spec`.

---

## State Machine

State machine is **unchanged** for Phase I. `used_speckit` flag in the state entry distinguishes which path is active; status names (`in_progress`, `proposal_open`, `implementing`, etc.) remain as-is. No new statuses.

---

## Backend Compatibility

All three backends work without changes — hunter invokes spec-kit by sending prompt templates via the existing `run_skill` machinery. The bedrock and claude backends have active tool use (`read_file`, `list_files`, `bash`) and can discover additional context from the worktree filesystem if prompted. Devin requires files to be staged explicitly (already handled by `copy_spec_refs`).

Bedrock end-to-end verification is a separate spec. Keep `speckit_enabled = false` on bedrock-backed instances until verified.

---

## Out of Scope

- Phases 1–3 (constitution, specify, clarify) — upstream / human-authored
- Auto-generating the constitution or spec — human-authored only
- Per-repo `speckit_enabled` flag — separate spec
- Removing `proposal_skill_path` / `impl_skill_path` from Config — stays as fallback
- Updating obsidian to produce spec-kit-compatible issues — follow-on spec
- Constitution staleness check against PM repo — `pm_source_sha` recorded in frontmatter for future use; not checked yet

---

## Risks

1. **Heavier token spend.** Two LLM calls (plan + implement) replace one each. Minor with prompt caching.
2. **BPA-Specs drift.** If BPA-Specs changes mid-flight, the pinned SHA in `plan.md` frontmatter anchors both plan and implement to the same snapshot.
3. **Epic → folder slug mismatch.** Epic naming conventions may not slugify cleanly. `speckit_epic_map` provides an escape hatch.

---

## Acceptance Criteria

**Phase I:**
1. Hunter picks up a story with a Jira epic that maps to a capability folder and opens a proposal PR containing `spec-refs/` + `plan.md` with `capability_specs_sha` in frontmatter.
2. For a story with no capability (Security, Tech Debt, etc.) — hunter falls back to `proposal_skill_path` with no error.
3. After the proposal PR merges, hunter runs spec-kit implement and opens an implementation PR reading from `spec-refs/` + `plan.md`.
4. `speckit_enabled = false` runs legacy flow unchanged.
5. `used_speckit` is recorded in state; `check_proposal_merged` respects it.
6. Tests cover: `resolve_capability_folder` (slug match, map fallback, no epic, no config), `read_bpa_specs_bundle` (happy path, hard fail, soft warn), `run_speckit_plan` (returns True/False), `process_issue` fork, `check_proposal_merged` fork.
7. Existing tests pass.

**Phase II:**
8. Predd running against a speckit proposal PR commits `tasks.md` to the branch and posts APPROVE review.
9. Predd detects plan inconsistency → posts REQUEST_CHANGES + `needs-replan` label.
10. Hunter re-plan loop increments counter, resets issue on next cycle; exhausted → `failed`.
11. Tests cover: analyze approve path, analyze inconsistent path, re-plan loop counter, exhaustion.

---

## Files Touched

| File | Phase |
|---|---|
| `predd.py` — Config fields, `process_pr` fork | I + II |
| `hunter.py` — new functions, `process_issue` fork, `check_proposal_merged` fork, re-plan loop | I + II |
| `prompts/speckit/plan.md`, `implement.md` | I |
| `prompts/speckit/analyze.md`, `tasks.md` | II |
| `test_hunter.py` | I + II |
| `CLAUDE.md` | I |

## Key Reused Functions (do not duplicate)

- `run_skill(cfg, skill_path, arguments, worktree)` — `hunter.py` ~line 835
- `setup_new_branch_worktree(...)` — `predd.py` ~line 1170
- `commit_skill_output / skill_has_commits` — `hunter.py`
- `gh_create_branch_and_pr / gh_issue_add_label / gh_issue_remove_label` — `hunter.py`
- `gh_pr_review(...)` — `predd.py`
- `_run_claude / _run_devin_skill / _run_bedrock_skill` — `predd.py`
- `fetch_jira_frontmatter` — `hunter.py` (already fetches epic fields; reuse whichever epic field has a value)
- `log_decision(...)` — both files
