# Migrate Hunter to Spec Kit Workflow

## Problem

Hunter today runs a two-stage skill workflow: proposal_skill produces a draft proposal PR, impl_skill produces an implementation PR. The skills are bespoke prompts that have to encode all the structure themselves — spec format, planning, task breakdown, traceability. There's no shared vocabulary with the rest of the SDD ecosystem.

GitHub's Spec Kit (`github/spec-kit`, MIT, 95k stars) defines that structure as a first-class set of artifacts: `constitution.md`, `spec.md`, `clarify`, `plan.md`, `tasks.md`, and `implement`. Adopting it gets us:
- A standard artifact layout that any spec-kit-aware tool can read
- Versioned templates maintained by GitHub, not by us
- Compatibility with the community-extension ecosystem (worktree isolation, plan review gates, retro analysis, etc.) we can adopt later without forking hunter

## Proposed Behaviour

### Stage Mapping (preserves the 2-PR gate model)

| Hunter stage today | Spec Kit equivalent | PR gate |
|---|---|---|
| `proposal_skill` produces a proposal | `/speckit.specify` + `/speckit.clarify` + `/speckit.plan` + `/speckit.tasks` produce four artifacts | **Specification PR** — `spec.md`, `plan.md`, `tasks.md` (+ optional `research.md`, `data-model.md`, `contracts/`) |
| `impl_skill` produces the implementation | `/speckit.implement` executes `tasks.md` | **Implementation PR** — code changes |

Five spec-kit checkpoints collapse into hunter's two existing PR gates. Human reviewer sees a richer artifact set in PR 1 (spec + plan + tasks together rather than a single proposal) but the merge/approval flow is unchanged.

### State Machine

Old:

```
new → in_progress → proposal_open → implementing → self_reviewing → ready_for_review → submitted
```

New:

```
new → specifying → spec_open → implementing → self_reviewing → ready_for_review → submitted
```

`specifying` covers all four spec-kit pre-implementation stages run sequentially in one worktree. Progress within `specifying` is tracked via decision-log events, not state transitions:

```
specify_started → specify_completed → clarify_completed → plan_completed → tasks_completed
```

The `proposal_*` state field names (`proposal_pr`, `proposal_branch`, `proposal_worktree`, `proposal_feedback`) rename to `spec_*` for clarity, but the old field names are read as aliases for backward compatibility on existing state files.

### Per-Repo Prerequisite Check

Before hunter processes any issue, it verifies the target repo has been initialized for Spec Kit:

```
<repo>/.specify/memory/constitution.md          # must exist
<repo>/.specify/templates/spec-template.md      # must exist
<repo>/.specify/templates/plan-template.md      # must exist
<repo>/.specify/templates/tasks-template.md     # must exist
<repo>/.specify/scripts/create-new-feature.sh   # must exist (or .ps1 on Windows hosts)
```

If any are missing, hunter:
1. Logs `log_decision("speckit_not_initialized", repo=repo)`
2. Posts a comment on the issue: `⚠️ This repo is not initialized for Spec Kit. Run 'specify init .' in the repo and add a constitution (run /speckit.constitution in your agent). Hunter will skip this issue until done.`
3. Adds label `{github_user}:speckit-uninitialized`
4. Skips the issue (does not claim, does not pick up again on next poll until the label is removed by a human after init)

Auto-running `specify init` is out of scope — the constitution needs human authorship.

### Specification Stage (replaces `process_issue`)

When hunter claims an issue:

1. **Branch and worktree** (unchanged pattern). New branch name: `{cfg.branch_prefix}/spec/{issue_number}-{slug}` (e.g. `usr/at/spec/377-port-linter-prs`). Worktree under `cfg.worktree_base` as today.

2. **Bootstrap the feature folder** by invoking spec-kit's `create-new-feature.sh` from inside the worktree:

```bash
./.specify/scripts/create-new-feature.sh "<issue-title>"
```

This creates `.specify/specs/NNN-<slug>/spec.md` (initial template) and switches the worktree's branch if spec-kit's script does its own branch creation. Hunter checks the resulting branch name and renames it to match `{cfg.branch_prefix}/spec/{issue_number}-{slug}` if the script's default doesn't match. (Spec-kit's branch naming and hunter's user-prefix convention need to coexist — we keep hunter's prefix for label/state correlation.)

3. **Run `/speckit.specify` equivalent.** Send the agent a prompt with the issue body as input and instructions to populate `spec.md`. The prompt structure mirrors what spec-kit's slash command sends — see "Prompt Templates" below.

4. **Run `/speckit.clarify` equivalent.** Drives the agent through structured clarification questions. Recorded in a Clarifications section of `spec.md`. Skippable via `cfg.speckit_skip_clarify = true` for prototype-mode work; default `false`.

5. **Run `/speckit.plan` equivalent.** Agent produces `plan.md` + supporting files (`research.md`, `data-model.md`, `contracts/`). The tech stack is inferred from the existing repo — no human input on plan tech choices, which differs from interactive spec-kit usage. If hunter wants the human to make the tech-stack call, that's a separate spec.

6. **Run `/speckit.tasks` equivalent.** Agent produces `tasks.md` with the task breakdown, dependency markers, parallel-execution markers.

7. **(Optional) Run `/speckit.analyze` equivalent.** Cross-artifact consistency check. Gated on `cfg.speckit_run_analyze = true`; default `false` (extra LLM cost, useful but not blocking).

8. **Commit and open Specification PR.** All artifacts in one commit: `spec: issue #N — <title>`. PR title `Spec: <title>`. Label `sdd-proposal` (kept for hunter's existing discovery via `gh_find_merged_proposal`). The merged-proposal discovery and downstream wiring don't need to change.

9. **State** → `spec_open`. Label `{github_user}:spec-open` on the issue (was `:proposal-open`; rename in the same change). Wait for human merge.

### Implementation Stage (replaces the existing implementation path)

When hunter detects a merged `sdd-proposal` PR for an issue (via existing `gh_find_merged_proposal`):

1. Create implementation branch `{cfg.branch_prefix}/impl/{issue_number}-{slug}` and worktree.

2. **Run `/speckit.implement` equivalent.** Agent reads `.specify/specs/NNN-<slug>/spec.md`, `plan.md`, `tasks.md` and executes the tasks. Hunter's `run_skill` is replaced with a `run_speckit_implement` function. The prompt structure includes paths to the three artifact files and the constitution.

3. Commit (`commit_skill_output`), validate non-empty diff (`skill_has_commits`), open implementation PR with label `sdd-implementation`. Self-review loop runs unchanged.

### Prompt Templates

Hunter ships prompt templates that approximate each spec-kit slash command for headless use. Stored in the predd repo at:

```
prompts/speckit/specify.md
prompts/speckit/clarify.md
prompts/speckit/plan.md
prompts/speckit/tasks.md
prompts/speckit/analyze.md
prompts/speckit/implement.md
```

Each is a Jinja-lite template (Python `str.format` with named placeholders — no new dep) that substitutes:
- `{issue_number}`, `{issue_title}`, `{issue_body}`, `{constitution_path}`, `{spec_dir}`, `{template_paths}` as relevant.

The substance of each prompt: instruct the agent to follow the spec-kit template at `<repo>/.specify/templates/<template>.md` and write the output to the correct path. The agent gets repo-local file access via the existing tool set (bedrock backend) or filesystem access (claude/devin CLIs).

### Replaces `cfg.proposal_skill_path` and `cfg.impl_skill_path`

These config knobs are deprecated. New knobs:

```toml
# Spec-Kit workflow
speckit_enabled = true                # set false to keep legacy skill workflow
speckit_prompt_dir = "/path/to/predd/prompts/speckit"  # default points at repo
speckit_skip_clarify = false          # skip the clarify stage (faster, less rigorous)
speckit_run_analyze = false           # run /speckit.analyze after tasks
```

When `speckit_enabled = false`, hunter uses the old `proposal_skill_path` / `impl_skill_path` flow unchanged. This gives us a feature flag to roll back if the new flow breaks. Once stable, the legacy path can be deleted.

`cfg.skill_path` (the PR-review skill used by predd and by hunter's self-review) stays — spec-kit doesn't replace post-implementation code review.

### Backend Compatibility

All three backends (`claude`, `devin`, `bedrock`) work without changes because hunter invokes spec-kit by:
- Running shell scripts (`create-new-feature.sh`) via subprocess — backend-agnostic
- Sending prompt templates to the agent via the existing `run_skill` machinery — backend-agnostic

The bedrock backend already has the tool set (`read_file`, `list_files`, `bash`) needed to navigate `.specify/` and write artifacts. No new tools required.

### Obsidian Output Adjustment

Obsidian's `_extract_and_write_specs` currently writes full spec markdown files into `spec/changes/` and hunter picks those up. With spec-kit:

- Obsidian writes one-line **issue descriptions** into `spec/changes/`, formatted as GitHub issue bodies. A separate small loop (or a new field on the obsidian output) files them as GitHub issues in the target repo.
- Hunter then picks up the issues normally and runs the spec-kit workflow on them.

This is a separate follow-on spec — not required for this one. Until obsidian is updated, its output keeps going to `spec/changes/` and the legacy flow handles it (when `speckit_enabled = false`) or is ignored (when `speckit_enabled = true`).

### Branch Prefix Reconciliation

Spec-kit's `create-new-feature.sh` creates branches named `NNN-<slug>` (e.g. `001-create-taskify`). Hunter's convention is `{cfg.branch_prefix}/<stage>/{issue_number}-<slug>`. We reconcile by:

1. Letting spec-kit's script create its branch.
2. Renaming it inside the script's worktree before opening the PR:
   ```bash
   git branch -m <speckit-name> <hunter-name>
   ```
3. Updating any internal spec-kit references to the branch name if needed (spec-kit doesn't appear to hard-code branch names in artifacts based on the docs).

### CLAUDE.md Updates

Document the new workflow in the conventions section. Add the constitution-required precondition. Update the spec inventory table to note spec-kit migration.

## Out of Scope

- Adopting any community spec-kit extension. Hunter drives spec-kit directly. Extension adoption is a separate decision per extension.
- Auto-generating the constitution. Human-authored only — it's the project's foundational document.
- Multi-feature parallel specification (spec-kit handles this via numbered feature folders; hunter is single-threaded per issue, which matches today).
- Updating obsidian to produce spec-kit-compatible issues. Follow-on spec.
- `predd analyze` / `obsidian analyze` themselves adopting spec-kit. They produce specs *about* the predd/hunter codebase; that's a different concern from hunter's per-issue workflow.
- Removing `proposal_skill_path` / `impl_skill_path` from Config. Stays for one release as a fallback path.

## Risks

1. **Heavier per-issue token spend.** Spec-kit runs four sequential LLM calls (specify, clarify, plan, tasks) where the old flow ran one. With prompt caching landed (the bedrock spec), the marginal cost is much lower — constitution + templates cache across calls. Still ~2-3x the cost of the old flow on the spec stage.

2. **Spec-kit's scripts may evolve.** The shell scripts in `.specify/scripts/` are versioned with the user's `specify init` run. If they change shape (rename, new arguments), hunter's invocations break. Pin spec-kit version via the repo's own `specify` install; do not invoke remote scripts.

3. **More artifacts in the spec PR means longer reviews.** Spec + plan + tasks is three documents to review instead of one. Could slow down the human gate. Counterargument: it makes the eventual implementation more predictable.

4. **Bedrock backend has not yet been verified end-to-end** (separate spec). If bedrock is the chosen backend for spec-kit work and the bedrock spec hasn't landed first, this spec is blocked on it.

## Acceptance Criteria

1. With a fresh repo where `specify init .` has been run and a constitution committed, hunter picks up an assigned issue and produces a Specification PR containing `spec.md`, `plan.md`, `tasks.md` (and `research.md`, `contracts/` if the plan generated them).
2. With a repo missing `.specify/memory/constitution.md`, hunter posts the prerequisite comment, applies the `speckit-uninitialized` label, and does not claim the issue.
3. After the Specification PR merges, hunter creates an Implementation PR by running `/speckit.implement` against the merged artifacts.
4. `speckit_enabled = false` runs the legacy `proposal_skill` / `impl_skill` flow with no changes from today's behavior.
5. State machine entries for new issues have status `specifying` then `spec_open` (not `in_progress` / `proposal_open`).
6. Existing state entries with `proposal_*` keys continue to advance correctly (alias read).
7. Decision-log events include `specify_started`, `specify_completed`, `clarify_completed`, `plan_completed`, `tasks_completed` for new-flow issues.
8. All three backends (`claude`, `devin`, `bedrock`) produce identical artifact structure for the same issue (content will differ; structure must not).
9. Tests in `test_hunter.py` cover: prerequisite check, prompt template substitution, the spec → impl transition reading the new fields, the legacy alias-read path, the feature flag toggle.
10. Existing tests pass.

## Files Touched

- `hunter.py` — new state machine, new `process_issue` body, new `check_proposal_merged` body (rename to `check_spec_merged`?), prerequisite check, prompt-template loading, Config additions.
- `predd.py` — Config knobs for `speckit_*`.
- `obsidian.py` — no required change in this spec (follow-on).
- `prompts/speckit/*.md` — new directory with six prompt templates.
- `test_hunter.py` — new tests.
- `CLAUDE.md` — workflow documentation.
- `README.md` — workflow section update.

## Migration

This is a big enough change to warrant a feature flag (`speckit_enabled`) plus a documented migration:

1. Land the spec with `speckit_enabled = false` as the default.
2. Initialize spec-kit on one target repo (`specify init .` + constitution).
3. Flip `speckit_enabled = true` for that repo (per-repo flag once the per-repo config spec lands; until then, global).
4. Run hunter on one real issue end-to-end. Compare artifact quality and token cost to the old flow.
5. If it works, flip the default to `true` and remove the legacy code path in a follow-on.

If it doesn't work, the flag flips back and we iterate without committing.
