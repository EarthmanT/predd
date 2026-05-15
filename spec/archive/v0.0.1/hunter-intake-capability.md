# Hunter: Intake Capability and Stories from Company Format

## Problem

The BPA-Specs repo holds capability docs in a company-specific format
(`capability.yaml`, `business_requirement.md`, `hld.md`, `business_spec.md`,
`notes.md`, `slices/<id>/slice.md`, `slices/<id>/slice.yaml`). The spec-kit
workflow hunter uses requires a different layout: `constitution.md`, `spec.md`,
and `stories/<jira-key>/spec.md`. These need to be populated before hunter can
plan or implement any story.

Writing them by hand duplicates work that already exists in structured form.

## Solution

Two new `hunter` CLI commands that read the company-format capability folder,
use the LLM to transform the content, and write spec-kit artifacts into
`capability_specs_path`.

```
hunter intake-capability <source-dir>
hunter intake-stories    <source-dir>
```

Both are idempotent — safe to re-run; they overwrite existing files.

---

## Source Format (Company)

```
<capability-id>-<slug>/
  capability.yaml          # id, title, slug, owners, jira_link
  business_requirement.md  # BR-001...  what the system must do
  business_spec.md         # AC-001...  testable acceptance criteria
  hld.md                   # ER-001...  components, dependencies, open questions
  notes.md                 # term definitions + original notes
  status.yaml              # lifecycle state (read-only, not transformed)
  slices/
    <slice-id>/
      slice.md             # Goal, Inputs, Scope, Out of Scope, Done When, Trace Links
      slice.yaml           # jira_epic, jira_stories[], impacted_repos, status
```

## Target Format (Spec-Kit)

```
<capability_specs_path>/
  <slug>/
    constitution.md        # non-negotiable constraints and term definitions
    spec.md                # full capability spec (BRs + ERs + ACs)
    stories/
      <JIRA-KEY>/
        spec.md            # per-story spec derived from slice + Jira
```

---

## `hunter intake-capability <source-dir>`

### What it does

1. Read `capability.yaml` → extract `slug`, `capability_id`, `title`
2. Read all source docs: `business_requirement.md`, `hld.md`,
   `business_spec.md`, `notes.md`
3. Determine output dir: `cfg.capability_specs_path / slug`; create if absent
4. Run **two LLM calls** (one per output file):
   - `constitution.md` — extract non-negotiable constraints, invariants, and
     term definitions
   - `spec.md` — full capability spec: BRs, ERs, ACs, dependencies, open
     questions
5. Write both files
6. Print a summary listing the Jira story keys found in all `slice.yaml` files
   so the user knows what to run `intake-stories` for next

### constitution.md extraction rules (in prompt)

Pull from the source material:
- **Term definitions** from `notes.md` — verbatim; the "do not improvise
  synonyms" rule applies
- **Architectural invariants** — rules the implementation must never violate,
  not features to build. Examples from the sample: tenant isolation (no
  cross-tenant reads at any layer), deterministic scoring (no LLM opinions in
  scores), LLM participation boundary (discovery only, not scoring or
  validation), audit logging on cross-tenant attempts
- **Hard thresholds** — values baked into requirements that must not drift
  (e.g. <50% confidence flag, <5 blueprint low-confidence notice,
  ≥3-consecutive feedback window)

Do NOT include: BRs as BRs, ERs, ACs, open questions, or delivery timelines.
Those go in `spec.md`.

### spec.md extraction rules (in prompt)

Combine all four source docs into one coherent document:
- All BRs (verbatim, with BR-NNN IDs preserved)
- All ERs with their `Satisfies`/`Consumes` links (verbatim, with ER-NNN IDs)
- All ACs (verbatim, with AC-NNN IDs and BR trace links)
- Dependencies table from `hld.md`
- Open questions from `hld.md` (as a clearly marked section)

Preserve IDs. Do not summarise or paraphrase — the LLM's job is to
restructure, not rewrite.

### Config

Uses existing `cfg.capability_specs_path`. No new config fields.

### CLI

```
hunter intake-capability ./bpa-specs/specs/23264-bpa-customer-inventory-trained-bp-generation
```

Output:
```
Capability: bpa-customer-inventory-trained-bp-generation
Output dir: ~/windsurf/projects/bpa-specs/specs/bpa-customer-inventory-trained-bp-generation
Writing constitution.md... done
Writing spec.md... done

Stories found in slices (run intake-stories to generate):
  DAP09A-1832  (s1-define-pattern-schema)
  DAP09A-1833  (s2-extraction-and-cli)
  ...
```

---

## `hunter intake-stories <source-dir>`

### What it does

For each slice under `slices/`:
1. Read `slice.yaml` → get `jira_stories[]`, `jira_epic`
2. Read `slice.md` → Goal, Scope, Out of Scope, Done When, Trace Links
3. For each Jira story key in `jira_stories[]`:
   a. Fetch story from Jira API (reuse existing `JiraClient` from hunter)
   b. Run one LLM call → produce the story spec (same content as `stories/<JIRA-KEY>/spec.md`)
   c. If story has no AC in Jira, add `> ⚠️ Thin story: no acceptance criteria
      found in Jira. Slice "Done When" used as fallback.` at top of file
   d. Write to `cfg.capability_specs_path / slug / stories / jira_key / spec.md`
   e. **Embed spec-kit artifacts into the GitHub issue body** (see below)

### GitHub issue body embedding

After generating the story spec, `intake-stories` finds the corresponding GitHub
issue for the Jira story key and updates its body to embed all three spec-kit
artifacts as structured HTML comment blocks. This makes the issue self-contained:
hunter can read everything it needs from the issue at proposal time without
requiring local `capability_specs_path` access.

**Embedding format** (appended to or replacing existing body):

```
<!-- speckit:constitution
<content of constitution.md>
-->

<!-- speckit:capability-spec
<content of spec.md (capability-level)>
-->

<!-- speckit:story-spec
<content of the story spec.md>
-->
```

**Finding the GitHub issue**: GitHub issues have the Jira story key in the
title (e.g. `DAP09A-1832: Story title` or `[DAP09A-1832] Story title`).
`intake-stories` searches the configured repos for an open issue whose title
contains the Jira key and updates it. Tech debt and security epics are excluded
by the user at issue-creation time — no filtering needed here.

If no matching GitHub issue is found, the story spec is still written to disk
and a warning is printed — the command does not fail.

**Idempotency**: if `<!-- speckit:constitution -->` blocks already exist in the
issue body, they are replaced (not appended again).

### Hunter reads from issue body at proposal time

When `run_speckit_plan()` picks up a speckit issue, it reads the embedded blocks
from the issue body and writes them to `spec-refs/` in the worktree:

- `spec-refs/constitution.md` ← `<!-- speckit:constitution -->`
- `spec-refs/capability-spec.md` ← `<!-- speckit:capability-spec -->`
- `spec-refs/story-spec.md` ← `<!-- speckit:story-spec -->`

This replaces the existing behavior of reading from `capability_specs_path` on
disk. The local path is used only as the write target for the generated files —
not as the source for proposal work.

If any block is missing from the issue body, `run_speckit_plan()` falls back to
reading from `capability_specs_path` on disk (same as today), with a warning.

### story spec.md content (in prompt)

Combine slice and Jira data:
- **What**: Goal from `slice.md` + Jira story summary
- **Scope**: Scope section from `slice.md`
- **Out of scope**: Out of Scope section from `slice.md`
- **Acceptance criteria**: Jira ACs if present, otherwise Done When from
  `slice.md`
- **Trace links**: BRs and ERs from `slice.md` Trace Links section
- **Impacted repos**: from `slice.yaml.impacted_repos`

Thin story handling: if the Jira story description is fewer than 50 words or
has no acceptance criteria, include the warning header and use the slice's
"Done When" as the sole acceptance criteria source. Do not hallucinate missing
requirements.

### Jira API usage

Reuses the existing `JiraClient` and `cfg.jira_*` config fields. Requires
`JIRA_API_TOKEN` env var (same as `ingest-jira-api`). Stories that 404 are
skipped with a warning; processing continues for remaining stories.

### CLI

```
hunter intake-stories ./bpa-specs/specs/23264-bpa-customer-inventory-trained-bp-generation
```

Output:
```
Slice s1-define-pattern-schema: 1 story
  DAP09A-1832  Writing stories/DAP09A-1832/spec.md... done
             Updating GitHub issue owner/repo#42... done
Slice s2-extraction-and-cli: 2 stories
  DAP09A-1890  Writing stories/DAP09A-1890/spec.md... done (⚠ thin story)
             Updating GitHub issue owner/repo#43... done
  DAP09A-1891  Writing stories/DAP09A-1891/spec.md... done
             ⚠ No GitHub issue found for DAP09A-1891 — skipping embed
...
```

---

## Implementation

### New functions in `hunter.py`

```python
def _read_capability_source(source_dir: Path) -> dict
```
Reads `capability.yaml` and all source MDs. Returns dict with keys:
`slug`, `capability_id`, `title`, `business_requirement`, `hld`,
`business_spec`, `notes`, `slices` (list of dicts with slice.md content,
slice.yaml fields, and jira_story_keys).

```python
def _run_intake_prompt(cfg: Config, prompt: str) -> str
```
Runs a plain-text prompt through the configured backend with no skill file.
Reuses `_run_skill_prompt` from predd (already imported).

```python
def intake_capability(cfg: Config, source_dir: Path) -> None
```
Orchestrates the capability intake: reads source, builds prompts, calls LLM,
writes output files, prints summary.

```python
def _embed_speckit_blocks(body: str, constitution: str, capability_spec: str, story_spec: str) -> str
```
Replaces or appends `<!-- speckit:constitution -->`, `<!-- speckit:capability-spec -->`,
and `<!-- speckit:story-spec -->` blocks in an issue body string. Returns the
updated body. Pure function — no I/O.

```python
def _find_github_issue_for_jira_key(cfg: Config, repos: list[str], jira_key: str) -> tuple[str, int] | None
```
Searches `repos` for an open GitHub issue whose title contains `[jira_key]`.
Returns `(repo, issue_number)` or `None` if not found.

```python
def intake_stories(cfg: Config, source_dir: Path) -> None
```
Orchestrates the stories intake: iterates slices, fetches from Jira, calls LLM
per story, writes output files, embeds spec-kit blocks into GitHub issues, prints
summary.

### Prompt templates

Two new files in `prompts/speckit/`:
- `intake-constitution.md` — system instructions for extracting constitution
- `intake-spec.md` — system instructions for extracting capability spec
- `intake-story.md` — system instructions for producing a story spec

Templates use `str.format()` placeholders (same pattern as existing speckit
prompts): `{slug}`, `{title}`, `{business_requirement}`, `{hld}`,
`{business_spec}`, `{notes}`, `{slice_goal}`, `{slice_scope}`,
`{slice_out_of_scope}`, `{slice_done_when}`, `{slice_trace_links}`,
`{jira_summary}`, `{jira_description}`, `{jira_acceptance_criteria}`,
`{impacted_repos}`.

### New CLI commands (Click)

```python
@hunter.command("intake-capability")
@click.argument("source_dir", type=click.Path(exists=True))
@click.pass_context
def cmd_intake_capability(ctx, source_dir): ...

@hunter.command("intake-stories")
@click.argument("source_dir", type=click.Path(exists=True))
@click.pass_context
def cmd_intake_stories(ctx, source_dir): ...
```

---

## Out of Scope

- Watching for new capabilities or stories automatically (follow-on)
- Updating existing spec-kit files when source docs change (follow-on)
- Validating that the generated spec-kit files are complete (that's what
  `speckit_run_analyze` does at review time)
- Creating GitHub issues from slices (that's `ingest-jira-api`)
- A combined `intake-all` command (YAGNI until there's a clear need)

---

## Acceptance Criteria

1. `hunter intake-capability <dir>` writes `constitution.md` and `spec.md`
   under `capability_specs_path/<slug>/` and prints the list of story keys
   found in slices.
2. `hunter intake-stories <dir>` writes `stories/<JIRA-KEY>/spec.md` for each
   story key found in `slice.yaml.jira_stories[]`, fetching from Jira API.
3. `hunter intake-stories <dir>` finds the matching GitHub issue for each Jira
   story key and updates its body with `<!-- speckit:constitution -->`,
   `<!-- speckit:capability-spec -->`, and `<!-- speckit:story-spec -->` blocks.
4. If no matching GitHub issue is found, a warning is printed and the story spec
   is still written to disk; the command does not fail.
5. Re-running `intake-stories` replaces existing speckit blocks rather than
   appending duplicates (idempotent).
6. `run_speckit_plan()` reads spec-kit artifact content from speckit blocks in
   the issue body when present, writing them to `spec-refs/` in the worktree.
   Falls back to `capability_specs_path` on disk if blocks are absent.
7. Thin stories (no Jira ACs, <50-word description) produce a file with the
   warning header and use slice "Done When" as ACs.
8. A story that 404s in Jira is skipped with a printed warning; remaining
   stories are processed.
9. Both commands are idempotent — re-running overwrites cleanly without error.
10. `speckit_enabled = false` or missing `capability_specs_path` exits with a
    clear error message rather than a crash.
11. Tests cover: `_read_capability_source` (parses all fields, handles missing
    optional files), `intake_capability` (calls LLM twice, writes two files),
    `intake_stories` (iterates slices, handles thin/404 stories, updates GitHub
    issue body), `_embed_speckit_blocks` (insert/replace blocks),
    `run_speckit_plan` reads from issue body blocks.
