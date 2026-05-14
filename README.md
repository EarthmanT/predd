# predd

Three daemons that run in the background and do your GitHub work:

| Daemon | What it does |
|--------|-------------|
| **predd** | Polls GitHub for open PRs, reviews them via AI, posts inline comments automatically |
| **hunter** | Picks up issues assigned to you, writes proposals, implements, self-reviews, closes on merge |
| **obsidian** | (planned) Observes patterns in logs/feedback, generates improvement specs |

---

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/your-org/predd ~/windsurf/projects/predd
chmod +x ~/windsurf/projects/predd/predd.py ~/windsurf/projects/predd/hunter.py
ln -s ~/windsurf/projects/predd/predd.py ~/.local/bin/predd
ln -s ~/windsurf/projects/predd/hunter.py ~/.local/bin/hunter

# 2. Run the interactive setup wizard
predd init

# 3. Start everything via systemd
cd ~/windsurf/projects/predd && ./start.sh
```

Prerequisites: Python 3.12+, `uv`, `gh` CLI (`gh auth login` once), and AWS credentials if using Bedrock.

---

## Setup

### Interactive wizard

`predd init` walks you through creating `~/.config/predd/config.toml`. It prompts for:

- GitHub username and repos
- Worktree base directory
- Skill file paths
- Backend selection (Bedrock recommended)
- Jira integration (optional)

Run `predd init --force` to overwrite an existing config.

### Manual config

Edit `~/.config/predd/config.toml` directly. One `[[repo]]` block per GitHub repo:

```toml
# Per-repo configuration
[[repo]]
name = "owner/repo"
predd = true
hunter = true
obsidian = true

# GitHub
github_user = "your-github-username"
worktree_base = "/home/you/pr-reviews"

# Skill paths
skill_path = "~/.windsurf/skills/pr-review/SKILL.md"
proposal_skill_path = "~/.windsurf/skills/proposal/SKILL.md"
impl_skill_path = "~/.windsurf/skills/impl/SKILL.md"

# Backend: "bedrock" (recommended), "claude", or "devin"
backend = "bedrock"
model = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
aws_profile = "default"
aws_region = "us-east-1"
bedrock_model = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"

# Trigger: "ready" (all open non-draft PRs) or "requested" (explicit reviewer only)
trigger = "ready"

# Jira integration (optional)
# jira_base_url = "https://jira.example.com"
# jira_api_enabled = true          # set JIRA_API_TOKEN env var
# jira_sprint_filter = "active"    # "active", "all", or "named:<sprint>"
# require_jira_conformance = true  # block issues missing epic/sprint
```

### Starting services

`start.sh` manages all three daemons as systemd user services:

```bash
./start.sh
```

This script:
1. Loads `JIRA_API_TOKEN` from `~/.bashrc` and writes it into a systemd service override
2. Runs `systemctl --user daemon-reload`
3. Restarts `predd`, `hunter`, and `obsidian` services

Individual service management:

```bash
systemctl --user start|stop|restart|status predd
systemctl --user start|stop|restart|status hunter
systemctl --user start|stop|restart|status obsidian
```

### Verify

```bash
predd config                  # show loaded config
gh pr list                    # confirm gh auth works
aws sts get-caller-identity   # confirm AWS creds (bedrock only)
```

---

## Features

### PR Reviews (predd)

- Automatically reviews all open PRs in watched repos (skips your own PRs and drafts)
- **Trigger modes:**
  - `trigger = "ready"` — review all open non-draft PRs (default)
  - `trigger = "requested"` — only PRs where you're an explicit reviewer
- **Re-review on new commits** — if head SHA changes on a submitted PR, predd reviews again
- **Auto-posts reviews** — review appears on GitHub immediately; no manual approval step
- Posts verdicts: `APPROVE`, `REQUEST_CHANGES`, or `COMMENT`

### Issue Implementation (hunter)

- Claims issues, runs proposal skill, opens a draft PR for review
- Once proposal is merged, automatically implements the issue
- **Self-review loop**: runs impl skill, self-reviews, fixes issues, re-reviews (up to `max_review_fix_loops`)
- **Auto-close on merge**: closes issue when impl PR merges
- **Resume & rollback**: survives crashes; rolls back stuck issues after `max_resume_retries`
- **Feedback collection**: captures PR review feedback for analysis
- **Auto-labeling**: applies `sdd-proposal` and `sdd-implementation` labels
- **PR title format**: `[JIRA-ID] Proposal/Impl - Issue name` (or `Proposal/Impl - Issue name` when no Jira key)

### Jira Integration

Set `jira_api_enabled = true` and provide `JIRA_API_TOKEN` env var. Hunter fetches issues directly from the Jira REST API and creates GitHub issues.

Additional Jira settings:

| Setting | Default | Notes |
|---------|---------|-------|
| `jira_base_url` | (none) | Jira instance URL |
| `jira_api_enabled` | `false` | Use Jira REST API |
| `jira_sprint_filter` | `active` | `active`, `all`, or `named:<sprint-name>` |
| `require_jira_conformance` | `true` | Block issues missing epic, sprint, or capability |

When `jira_api_enabled = true`, proposal and impl PRs get a Jira metadata table at the top of the PR body (epic, sprint, capability, Jira link).

### Status Dashboard

Web UI at **http://localhost:8080**:
- Real-time summary cards by status
- Recent activity log (color-coded by event type)
- JSON API at `/api/status`
- Auto-refresh every 30 seconds

### Failure Handling

- **Failure comments**: posts GitHub comments when skills crash or produce no output
- **Failure labels**: adds `{github_user}:predd-failed` or `hunter-failed` labels
- **Stale cleanup**: removes failure records older than `failure_cleanup_days`
- **Decision logging**: records all events in JSONL logs

---

## Backends

### Bedrock (Recommended)

```toml
backend = "bedrock"
model = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
aws_profile = "default"
aws_region = "us-east-1"
bedrock_model = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
```

Uses standard AWS credential chain (env vars, `~/.aws/credentials`, SSO, IAM role).

### Claude CLI

```toml
backend = "claude"
model = "claude-opus-4-6"
```

Requires `claude` CLI. Uses OAuth (strips `ANTHROPIC_API_KEY`).

### Devin

```toml
backend = "devin"
model = "swe-1.6"
```

Requires `devin` CLI.

---

## Configuration Reference

### Core

| Setting | Default | Notes |
|---------|---------|-------|
| `github_user` | (required) | Your GitHub username |
| `worktree_base` | (required) | Directory for git worktrees |
| `poll_interval` | `90` | Polling interval (seconds) |
| `backend` | `bedrock` | `bedrock`, `claude`, or `devin` |
| `trigger` | `ready` | `ready` or `requested` |

### `[[repo]]` block fields

| Field | Default | Notes |
|-------|---------|-------|
| `name` | (required) | `owner/repo` slug |
| `predd` | `true` | Watch this repo for PR reviews |
| `hunter` | `true` | Watch this repo for issues |
| `obsidian` | `true` | Include in obsidian observations |

### Skill paths

| Setting | Default |
|---------|---------|
| `skill_path` | `~/.windsurf/skills/pr-review/SKILL.md` |
| `proposal_skill_path` | `~/.windsurf/skills/proposal/SKILL.md` |
| `impl_skill_path` | `~/.windsurf/skills/impl/SKILL.md` |

Skills in `.windsurf/skills/` or `.skills/` within the watched repo will be used if found.

### Hunter

| Setting | Default | Notes |
|---------|---------|-------|
| `branch_prefix` | `usr/at` | Prefix for hunter-created branches |
| `max_review_fix_loops` | `1` | Self-review iterations before flagging human |
| `auto_review_draft` | `false` | Self-review draft impl PRs |
| `max_resume_retries` | `2` | Retries before rolling back stuck issues |
| `max_new_issues_per_cycle` | `1` | New issues per repo per poll |
| `orphan_scan_interval` | `10` | Poll cycles between orphan label scans |
| `auto_label_prs` | `true` | Auto-apply sdd-proposal/implementation labels |
| `collect_pr_feedback` | `true` | Capture PR review feedback |

### Failure handling

| Setting | Default | Notes |
|---------|---------|-------|
| `comment_on_failures` | `true` | Post GitHub comments on failures |
| `predd_failure_label` | `{github_user}:predd-failed` | Label for predd failures |
| `failure_cleanup_days` | `7` | Age before cleanup (days) |
| `failure_cleanup_interval` | `10` | Poll cycles between cleanup runs |

### Status page

| Setting | Default | Notes |
|---------|---------|-------|
| `status_server_enabled` | `true` | Start web dashboard |
| `status_port` | `8080` | Dashboard port |
| `status_refresh_interval` | `30` | Auto-refresh interval (seconds) |

---

## CLI Commands

### predd

```bash
predd start [--once]             # Start daemon (--once for single poll)
predd init [--force]             # Interactive config wizard
predd config                     # Show loaded configuration
predd config set <key> <value>   # Update a single config field
predd list                       # List pending reviews (awaiting_approval state)
predd show <pr>                  # Show draft review for a PR
predd approve <pr>               # Submit draft as approval
predd comment <pr>               # Submit draft as comment-only
predd request-changes <pr>       # Submit draft as request-changes
predd reject <pr>                # Discard draft, mark as reviewed
predd observe                    # Read decision logs, write observation notes
predd analyze [--model M] [--days N]  # Analyze observations, write specs
predd status-server              # Start status server standalone
```

`predd config set` supports these keys: `github_user`, `worktree_base`, `backend`, `model`, `trigger`, `max_review_fix_loops`, `auto_review_draft`, `max_resume_retries`, `max_new_issues_per_cycle`, `orphan_scan_interval`, `auto_label_prs`, `collect_pr_feedback`, `branch_prefix`, `jira_base_url`, `jira_api_enabled`, `jira_sprint_filter`.

### hunter

```bash
hunter start [--once]            # Start daemon
hunter init [--force]            # Interactive config wizard (same wizard as predd init)
hunter status                    # Summary counts by status
hunter list                      # Full state (JSON)
hunter show <issue>              # Show state for a specific issue
hunter status-server             # Start status server standalone
```

---

## Hunter Issue State Machine

```
new
 |
in_progress       (proposal skill running, worktree created)
 |
proposal_open     (proposal PR open, waiting for merge)
 |
implementing      (proposal merged, impl skill running)
 |
self_reviewing    (impl PR exists, reviewing it)
 |-- implementing (issues found, fix loop; max max_review_fix_loops)
 |
ready_for_review  (review approved or loops exhausted)
 |
submitted         (impl PR merged, issue closed)

failed            (stuck; retryable via rollback)
awaiting_verification  (legacy, treated as terminal)
```

Issue labels:
- `{github_user}:in-progress` — claiming & proposal stage
- `{github_user}:proposal-open` — proposal PR open
- `{github_user}:implementing` — implementation in progress

PR labels (if `auto_label_prs = true`):
- `sdd-proposal` — PR is a proposal
- `sdd-implementation` — PR is an implementation

---

## Predd PR State Machine

| Status | Meaning |
|--------|---------|
| `reviewing` | Skill subprocess running |
| `submitted` | Review posted; re-reviewed if head SHA changes |
| `rejected` | Skipped (own PR, draft, already reviewed) |
| `failed` | Crash or no output from skill |
| `awaiting_approval` | Legacy; not used in current auto-post mode |

---

## Logs and Debugging

### Monitor logs

```bash
tail -f ~/.config/predd/log.txt           # predd activity
tail -f ~/.config/predd/hunter-log.txt    # hunter activity
tail -f ~/.config/predd/obsidian-log.txt  # obsidian activity
```

Logs rotate at 10 MB (max 3 files each).

### Decision logs

JSONL format at `~/.config/predd/decisions.jsonl` (predd) and `~/.config/predd/hunter-decisions.jsonl` (hunter):

```bash
jq 'select(.event == "pr_review_failed")' ~/.config/predd/decisions.jsonl
jq 'select(.ts > "2026-05-01")' ~/.config/predd/hunter-decisions.jsonl
```

Events: `pr_review_started`, `pr_review_posted`, `pr_review_failed`, `pr_skip`, `issue_pickup`, `proposal_created`, `proposal_merged`, `impl_created`, `issue_closed`, `rollback`, `pr_feedback`, `claim_failed`, `skill_no_commits`, `jira_conformance_failed`.

### State files

```bash
cat ~/.config/predd/state.json          # predd PR state (key: owner/repo#N)
cat ~/.config/predd/hunter-state.json   # hunter issue state (key: owner/repo!N)
```

### Status API

```bash
curl http://localhost:8080/api/status | jq
```

---

## Troubleshooting

### Config changes not picked up

Restart the affected service:

```bash
systemctl --user restart predd
systemctl --user restart hunter
```

Or re-run `./start.sh` to restart everything.

### "Review skill produced no output"

1. Confirm skill file exists at the configured path
2. Confirm prompt structure: task must come before skill instructions
3. Check logs: `tail -f ~/.config/predd/log.txt`

### Hunter stuck on `proposal_open` or `implementing`

```bash
hunter list | jq 'to_entries | map(select(.value.status == "proposal_open"))'
```

Manually merge the relevant PR, or reset the entry to `failed` in `~/.config/predd/hunter-state.json` to trigger a retry on the next poll cycle.

### AWS Bedrock auth failure

```bash
aws sts get-caller-identity
aws configure list --profile <profile>
aws bedrock list-foundation-models --region <region>
```

### Jira API not ingesting

- Confirm `JIRA_API_TOKEN` is set: `echo $JIRA_API_TOKEN`
- Re-run `./start.sh` to reload the token into the systemd override
- Confirm `jira_api_enabled = true` in config: `predd config`

---

## Development

### Testing

```bash
uv run --with pytest pytest test_pr_watcher.py test_hunter.py -q

# With coverage
uv run --with pytest --with pytest-cov pytest test_hunter.py --cov=hunter
```

Target: 80%+ coverage. Tests use `unittest.mock` — no real GitHub calls.

### Architecture

See `CLAUDE.md` for detailed architecture, design decisions, and conventions.

Implemented specs: `spec/archive/v0.0.1/`
Pending specs: `spec/changes/`

---

## License

MIT
