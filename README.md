# predd

Two background daemons that automate GitHub work:

- **predd** — watches your repos and posts AI code reviews on every open PR
- **hunter** — picks up GitHub issues assigned to you, writes a proposal, implements it, self-reviews, and closes the issue when the PR merges

A third daemon (**obsidian**) is planned but not yet active.

---

## What you need before starting

| Requirement | Why |
|-------------|-----|
| Python 3.12+ and `uv` | Both tools run as `uv` scripts |
| `gh` CLI, authenticated | All GitHub operations go through `gh` |
| AWS credentials | If using Bedrock backend (recommended) |
| `anthropic[bedrock]` Python package | Bedrock calls: `pip install 'anthropic[bedrock]'` |
| Windsurf skill files | The AI prompts that drive PR reviews and issue implementation (see below) |
| Jira API token | Only needed if using Jira integration or `intake-stories` |

### Skill files

Skills are markdown prompt files that tell the AI what to do. You need three:

| Skill | Config key | What it does |
|-------|-----------|--------------|
| PR review | `skill_path` | Runs on every open PR; posts inline comments |
| Proposal | `proposal_skill_path` | Writes a design doc / proposal PR for an issue |
| Implementation | `impl_skill_path` | Writes the code for an issue after the proposal merges |

These live in your Windsurf skills directory (typically `~/.windsurf/skills/`) or in a `.skills/` folder inside the repo being watched. You need to create or obtain these files separately — they're not bundled with predd.

---

## Installation

```bash
git clone https://github.com/your-org/predd ~/windsurf/projects/predd
chmod +x ~/windsurf/projects/predd/predd.py ~/windsurf/projects/predd/hunter.py
ln -s ~/windsurf/projects/predd/predd.py ~/.local/bin/predd
ln -s ~/windsurf/projects/predd/hunter.py ~/.local/bin/hunter

# Generate config
predd init
```

`predd init` creates `~/.config/predd/config.toml` and walks you through the required fields. Edit that file directly afterwards to add Jira or Spec Kit settings.

---

## Starting the daemons

```bash
./start.sh        # starts predd and hunter as systemd user services
```

Or run manually:

```bash
predd start       # Ctrl-C to stop gracefully; second Ctrl-C force-kills
hunter start
```

Use `--once` to run a single poll cycle and exit — useful for testing:

```bash
predd start --once
hunter start --once
```

---

## How predd works

1. Every `poll_interval` seconds, predd lists all open non-draft PRs in your watched repos
2. Skips PRs you authored
3. For each unreviewed PR, runs the review skill and posts the result to GitHub
4. If new commits arrive on a reviewed PR, it re-reviews automatically

**Trigger modes** (set via `trigger` in config):
- `ready` — review all open non-draft PRs (default)
- `requested` — only PRs where you're explicitly added as reviewer

---

## How hunter works

Hunter manages the full lifecycle of a GitHub issue, from pickup to close.

### The lifecycle

```
Issue assigned to you in GitHub
        ↓
hunter claims it (applies label, creates worktree)
        ↓
Runs proposal skill → opens draft PR
        ↓
You review and merge the proposal PR
        ↓
hunter runs implementation skill → opens impl PR
        ↓
hunter self-reviews, fixes issues (up to max_review_fix_loops)
        ↓
You review and merge the impl PR
        ↓
hunter closes the issue
```

### What hunter picks up

Hunter only acts on issues that are:
- Assigned to you (`@me` in GitHub)
- Open
- Not already in its state file

If you're using Jira integration, hunter also checks sprint conformance before picking up an issue. Issues that aren't in an active sprint are skipped.

### Jira integration

When `jira_api_enabled = true`, hunter polls your Jira projects and creates GitHub issues from Jira tickets. Issues only get created for tickets that meet the conformance rules (has an epic, is in an active sprint, correct issue type).

Set `JIRA_API_TOKEN` in your environment (add it to `~/.bashrc` and re-run `./start.sh` to pick it up in the service).

---

## Using BPA-Specs with hunter (Spec Kit)

If you have a BPA-Specs repository with capability specs, hunter can use them to drive proposals and implementations with much better context than a generic skill.

### The workflow

**One-time per capability** (before hunter runs):

```bash
# Step 1: Transform BPA-Specs source into hunter-readable artifacts
hunter intake-capability /path/to/bpa-specs/specs/<capability-folder>

# Step 2: Generate per-story spec files
hunter intake-stories /path/to/bpa-specs/specs/<capability-folder>
```

These commands read the raw capability source material (`capability.yaml`, `business_requirement.md`, `hld.md`, etc.) and produce:
- `constitution.md` — term definitions and architectural invariants
- `spec.md` — full requirements and acceptance criteria
- `stories/<JIRA-KEY>/spec.md` — one spec per story

**Then, asynchronously:**

Stories get triaged into sprints and assigned to engineers. Hunter doesn't pick up an issue until it's assigned to you and in an active sprint — so there's no rush to coordinate the intake run with the sprint cycle.

**When hunter picks up an issue:**

It looks up which capability folder corresponds to the issue's Jira epic, copies the relevant spec files into the branch, and uses them to drive the proposal and implementation. No extra steps needed.

### Intake warnings

- **"thin story"** — the Jira ticket has fewer than 50 words or no acceptance criteria. The spec file is still written, with a warning header. Consider fleshing out the Jira ticket before hunter picks it up.
- **"no GitHub issue found"** — the story hasn't been ingested into GitHub yet. This is normal — the spec file lands on disk, and `intake-stories` can be re-run later once the GitHub issue exists to embed the spec into the issue body.

### Spec Kit config

```toml
speckit_enabled = true
capability_specs_path = "/path/to/bpa-specs/specs"
speckit_prompt_dir = "/path/to/predd/prompts/speckit"   # defaults to prompts/speckit in this repo
```

Add `speckit_epic_map` if your Jira epic keys don't match the capability folder slugs:

```toml
[speckit_epic_map]
DAP09A-123 = "my-capability-slug"
```

---

## Configuration Reference

Config lives at `~/.config/predd/config.toml`, shared by both daemons.

### Core

| Setting | Default | Notes |
|---------|---------|-------|
| `github_user` | (required) | Your GitHub username |
| `worktree_base` | (required) | Directory where git worktrees are created |
| `poll_interval` | `90` | Seconds between polls |
| `backend` | `bedrock` | `bedrock`, `claude`, or `devin` |
| `trigger` | `ready` | `ready` or `requested` |

### `[[repo]]` block

One block per GitHub repo:

```toml
[[repo]]
name = "owner/repo"    # required
predd = true           # watch for PR reviews
hunter = true          # watch for issues
```

### Skill paths

| Setting | Default |
|---------|---------|
| `skill_path` | `~/.windsurf/skills/pr-review/SKILL.md` |
| `proposal_skill_path` | `~/.windsurf/skills/proposal/SKILL.md` |
| `impl_skill_path` | `~/.windsurf/skills/impl/SKILL.md` |

### Backends

**Bedrock (recommended)**
```toml
backend = "bedrock"
aws_profile = "default"
aws_region = "us-east-1"
bedrock_model = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
```

**Claude CLI**
```toml
backend = "claude"
model = "claude-opus-4-6"
```

**Devin**
```toml
backend = "devin"
model = "swe-1.6"
```

### Hunter

| Setting | Default | Notes |
|---------|---------|-------|
| `branch_prefix` | `usr/at` | Prefix for hunter-created branches |
| `max_review_fix_loops` | `1` | Self-review iterations before flagging human |
| `auto_review_draft` | `false` | Self-review draft impl PRs |
| `max_resume_retries` | `2` | Retries before rolling back stuck issues |
| `max_new_issues_per_cycle` | `1` | New issues per repo per poll |
| `auto_label_prs` | `true` | Apply `sdd-proposal` / `sdd-implementation` labels |
| `collect_pr_feedback` | `true` | Capture PR review feedback |

### Jira

| Setting | Default | Notes |
|---------|---------|-------|
| `jira_base_url` | (none) | Your Jira instance URL |
| `jira_api_enabled` | `false` | Enable Jira REST API integration |
| `jira_projects` | `[]` | Jira project keys to poll |
| `jira_sprint_filter` | `active` | `active`, `all`, or `named:<sprint-name>` |
| `require_jira_conformance` | `true` | Skip issues missing epic or sprint |

### Spec Kit

| Setting | Default | Notes |
|---------|---------|-------|
| `speckit_enabled` | `false` | Use BPA-Specs artifacts for proposals/impls |
| `capability_specs_path` | (none) | Path to BPA-Specs `specs/` folder |
| `speckit_prompt_dir` | `<repo>/prompts/speckit` | Prompt templates for plan and implement |
| `speckit_epic_map` | `{}` | Manual epic key → folder slug overrides |

### Failure handling

| Setting | Default | Notes |
|---------|---------|-------|
| `comment_on_failures` | `true` | Post GitHub comment when a skill crashes |
| `predd_failure_label` | `{github_user}:predd-failed` | Label applied on failure |
| `failure_cleanup_days` | `7` | Remove failure records older than this |

### Status dashboard

| Setting | Default | Notes |
|---------|---------|-------|
| `status_server_enabled` | `true` | Enable web dashboard |
| `status_port` | `8080` | Dashboard port |

---

## CLI reference

### predd

```bash
predd start [--once]             # Start daemon
predd init [--force]             # Interactive setup wizard
predd config                     # Show loaded config
predd list                       # Pending reviews
predd observe                    # Write observation notes from logs
predd analyze [--model M] [--days N]  # Analyze observations, write specs
predd status-server              # Start dashboard standalone
```

### hunter

```bash
hunter start [--once]            # Start daemon
hunter init [--force]            # Interactive setup wizard
hunter status                    # Counts by status
hunter list                      # Full state (JSON)
hunter show <issue>              # State for a specific issue
hunter intake-capability <dir>   # Transform BPA-Specs source → constitution.md + spec.md
hunter intake-stories <dir>      # Generate story spec files, embed into GitHub issues
hunter status-server             # Start dashboard standalone
```

---

## Logs and debugging

```bash
# Live logs
tail -f ~/.config/predd/log.txt
tail -f ~/.config/predd/hunter-log.txt

# Decision log (structured JSONL)
jq 'select(.event == "pr_review_failed")' ~/.config/predd/decisions.jsonl
jq 'select(.ts > "2026-05-01")' ~/.config/predd/hunter-decisions.jsonl

# State files
cat ~/.config/predd/state.json          # predd: key = owner/repo#N
cat ~/.config/predd/hunter-state.json   # hunter: key = owner/repo!N

# Status API
curl http://localhost:8080/api/status | jq
```

---

## Troubleshooting

**Hunter stuck on `proposal_open`**
The proposal PR needs to be merged manually before hunter will continue. Once merged, hunter picks up on the next poll.

**Hunter stuck on `implementing` or `self_reviewing`**
```bash
hunter list | jq 'to_entries | map(select(.value.status == "implementing"))'
```
Edit `~/.config/predd/hunter-state.json` and set the status to `failed` to trigger a retry on the next poll.

**"Review skill produced no output"**
Check that the skill file exists at the configured path and that the file is readable. Check `tail -f ~/.config/predd/log.txt` for the full error.

**Jira not ingesting**
```bash
echo $JIRA_API_TOKEN          # should be non-empty
predd config                  # confirm jira_api_enabled = true
```
Re-run `./start.sh` after setting `JIRA_API_TOKEN` in `~/.bashrc` — the token needs to be injected into the systemd service environment.

**AWS Bedrock auth failure**
```bash
aws sts get-caller-identity
aws configure list --profile <profile>
```

---

## Development

```bash
# Run tests
uv run --with pytest pytest test_pr_watcher.py test_hunter.py -q

# With coverage
uv run --with pytest --with pytest-cov pytest test_hunter.py --cov=hunter
```

See `CLAUDE.md` for architecture details, design decisions, and contribution conventions.
