# predd

Two daemons that run in the background and do your GitHub work:

- **predd** — reviews every open PR assigned to you, posts inline comments via AI
- **hunter** — picks up issues assigned to you, writes proposals, implements them, self-reviews
- **obsidian** — observes patterns in decision logs, generates improvement specs (self-improvement loop)

---

## Quick Start

```bash
# Install
git clone https://github.com/your-org/predd ~/windsurf/projects/predd
chmod +x ~/windsurf/projects/predd/predd.py ~/windsurf/projects/predd/hunter.py
ln -s ~/windsurf/projects/predd/predd.py ~/.local/bin/predd
ln -s ~/windsurf/projects/predd/hunter.py ~/.local/bin/hunter

# Configure
predd start --once   # generates ~/.config/predd/config.toml then exits
# Edit config.toml with your repos and paths

# Run
tmux new -s predd && predd start  # Ctrl-B d to detach
tmux new -s hunter && hunter start
```

---

## Setup (tell Windsurf: "set this up for me")

> Clone this repo, then set up predd and hunter for my GitHub account. My username is `<your-github-username>`, my repos are `["owner/repo"]`, my worktrees go in `~/pr-reviews`, and my pr-review skill is at `<path-to-skill>`.

Windsurf will run the steps below.

---

## Manual Setup

### Prerequisites

- Python 3.12+ and `uv`
- `gh` CLI — run `gh auth login` once
- **For Bedrock backend:** AWS credentials configured (profile or env vars)
- **For Claude backend:** `claude` CLI authenticated
- **For Devin backend:** `devin` CLI authenticated (or use claude/bedrock instead)

### Configuration

First run generates a template:

```bash
predd start --once
```

Edit `~/.config/predd/config.toml`:

```toml
# Repos
repos = ["owner/repo"]

# GitHub
github_user = "your-github-username"

# Paths
worktree_base = "/home/you/pr-reviews"
skill_path = "/path/to/pr-review/SKILL.md"
proposal_skill_path = "/path/to/proposal/SKILL.md"
impl_skill_path = "/path/to/impl/SKILL.md"

# Backend: "bedrock" (recommended), "claude", or "devin"
backend = "bedrock"
model = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"

# Bedrock settings (when backend = "bedrock")
aws_profile = "default"
aws_region = "us-east-1"
bedrock_model = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"

# Polling
poll_interval = 120  # seconds

# Status page
status_server_enabled = true
status_port = 8080
status_refresh_interval = 30  # seconds

# Failure handling
comment_on_failures = true
failure_cleanup_days = 7
failure_cleanup_interval = 10  # poll cycles
```

Full config reference: see `CLAUDE.md`

### Run

```bash
# Start predd (PR reviews)
tmux new -s predd
predd start
# Ctrl-B then d

# Start hunter (issue proposals & implementations)
tmux new -s hunter
hunter start
# Ctrl-B then d

# View status (web UI)
open http://localhost:8080  # or curl http://localhost:8080/api/status

# Monitor logs
tail -f ~/.config/predd/log.txt
tail -f ~/.config/predd/hunter-log.txt

# Check state
predd list          # pending PR reviews
hunter status       # issue pipeline summary
hunter list         # full issue state (JSON)
```

### Verify

```bash
predd config         # show loaded config
gh pr list           # confirm gh auth works
aws sts get-caller-identity  # confirm AWS creds (bedrock only)
```

---

## Features

### PR Reviews (predd)

- Automatically reviews all open PRs assigned to you
- Supports **trigger modes**:
  - `trigger = "ready"` — review all open non-draft PRs (default)
  - `trigger = "requested"` — only PRs where you're an explicit reviewer
- **Re-review on new commits** — if PR head SHA changes, predd reviews again
- **Configurable backends**: Bedrock, Claude CLI, or Devin
- Posts verdicts: `APPROVE`, `REQUEST_CHANGES`, or `COMMENT`

### Issue Implementation (hunter)

- Claims issues, runs proposal skill, opens draft PR for review
- Once proposal is merged, automatically implements the issue
- **Self-review loop**: runs impl skill, self-reviews, fixes issues, re-reviews (up to `max_review_fix_loops`)
- **Auto-close on merge**: closes issue when impl PR merges
- **Resume & rollback**: survives crashes; rolls back stuck issues after `max_resume_retries` attempts
- **Feedback collection**: captures PR review feedback for analysis
- **Auto-labeling**: applies `sdd-proposal` and `sdd-implementation` labels

### Failure Handling

- **Failure comments**: Posts GitHub comments when skills crash or produce no output
- **Failure labels**: Adds `predd-failed` or `hunter-failed` labels for easy filtering
- **Stale failure cleanup**: Removes failures older than `failure_cleanup_days` (default 7 days)
- **Decision logging**: Records all failures in JSONL logs for analysis

### Status Dashboard

Web UI at **http://localhost:8080**:
- Real-time summary cards (reviewing, submitted, failed, etc.)
- Modal details showing all PRs/issues in each status
- Recent activity log with color-coded events (green=success, red=fail, blue=info, yellow=pending)
- Dark mode support (auto-detects system preference)
- JSON API at `/api/status` for programmatic access
- Auto-refresh every 30 seconds (configurable)

### Self-Improvement Loop (obsidian)

Experimental feature for continuous improvement:

```bash
# Manual observation
predd observe   # reads decision logs, writes daily observation notes

# Manual analysis
predd analyze --model claude-opus-4-6 --days 7  # analyzes observations, generates improvement specs

# Automated (if configured)
# Will run hourly observe and daily analyze with tight intervals for testing
```

Observations are stored at `~/.config/predd/obsidian/`

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

- AWS-managed Claude with agentic tool use
- Supports any Bedrock Claude model
- Uses standard AWS credential chain (env vars, `~/.aws/credentials`, SSO, IAM role)
- No rate limiting, predictable costs

### Claude CLI

```toml
backend = "claude"
model = "claude-opus-4-6"
```

- Uses `claude` CLI (requires subscription)
- Cheaper per-token but subject to rate limits
- Good for testing

### Devin (Legacy)

```toml
backend = "devin"
model = "swe-1.6"
```

- Requires `devin` CLI
- Rate-limited, slower

---

## Configuration Reference

### Core Settings

| Setting | Default | Notes |
|---------|---------|-------|
| `repos` | (required) | Repos watched by both predd and hunter |
| `predd_only_repos` | `[]` | Repos watched by predd only |
| `hunter_only_repos` | `[]` | Repos watched by hunter only |
| `github_user` | (required) | Your GitHub username |
| `worktree_base` | (required) | Directory for git worktrees |
| `poll_interval` | `120` | Polling interval (seconds) |

### Skill Paths

| Setting | Default |
|---------|---------|
| `skill_path` | `~/.windsurf/skills/pr-review/SKILL.md` |
| `proposal_skill_path` | `~/.windsurf/skills/proposal/SKILL.md` |
| `impl_skill_path` | `~/.windsurf/skills/impl/SKILL.md` |

### Predd Settings

| Setting | Default | Notes |
|---------|---------|-------|
| `backend` | `bedrock` | `bedrock`, `claude`, or `devin` |
| `model` | Depends on backend | Model ID or name |
| `trigger` | `ready` | `ready` or `requested` |

### Hunter Settings

| Setting | Default | Notes |
|---------|---------|-------|
| `branch_prefix` | `usr/at` | Prefix for hunter-created branches |
| `max_review_fix_loops` | `1` | Self-review iterations before flagging human |
| `auto_review_draft` | `false` | Auto-review draft impl PRs? |
| `max_resume_retries` | `2` | Retries before rolling back stuck issues |
| `max_new_issues_per_cycle` | `1` | New issues per repo per poll |
| `orphan_scan_interval` | `10` | Poll cycles between orphan label scans (0 = startup only) |
| `auto_label_prs` | `true` | Auto-apply sdd-proposal/implementation labels |
| `collect_pr_feedback` | `true` | Capture PR review feedback? |

### Failure Handling

| Setting | Default | Notes |
|---------|---------|-------|
| `comment_on_failures` | `true` | Post GitHub comments on failures? |
| `predd_failure_label` | `{github_user}:predd-failed` | Label for predd failures |
| `failure_cleanup_days` | `7` | Age before cleanup (days) |
| `failure_cleanup_interval` | `10` | Poll cycles between cleanup runs |

### Status Page

| Setting | Default | Notes |
|---------|---------|-------|
| `status_server_enabled` | `true` | Start web dashboard? |
| `status_port` | `8080` | Dashboard port |
| `status_refresh_interval` | `30` | Auto-refresh interval (seconds) |

### Obsidian (Self-Improvement)

| Setting | Default | Notes |
|---------|---------|-------|
| `observe_interval` | `600` | Seconds between observe runs (testing) |
| `analyze_interval` | `600` | Seconds between analyze runs (testing) |
| `fix_interval` | `1200` | Seconds between fix attempts (testing) |
| `analyze_days` | `7` | Days of observations to analyze |
| `analyze_model` | `claude-opus-4-7` | Model for analysis |

---

## CLI Commands

### predd

```bash
predd start [--once]          # Start daemon (--once for single poll)
predd list                    # List pending PR reviews
predd show <pr>               # Show draft review
predd config                  # Show loaded configuration
predd status-server           # Start status server standalone
predd observe                 # Observe patterns in decision logs
predd analyze [--model M] [--days N]  # Analyze observations, generate specs
```

### hunter

```bash
hunter start [--once]         # Start daemon
hunter status                 # Summary counts by status
hunter list                   # Full state (JSON)
hunter claim <issue>          # Manually claim issue
hunter skip <issue>           # Skip issue
hunter rollback <issue>       # Reset issue to failed state for retry
```

---

## Day-to-Day

**predd** — Silently reviews PRs as they come in. Check `predd list` or the status dashboard to see what's been reviewed. When it fails, posts a comment on the PR.

**hunter** — When you're assigned an issue, it claims it, proposes a solution (draft PR), and waits for your approval. Once the proposal is merged, it implements the issue and self-reviews. When merged, it closes the issue.

**Graceful shutdown:**
- **One Ctrl-C**: Wait for current task to finish, then exit cleanly
- **Two Ctrl-C** (or SIGTERM): Force-quit and roll back in-flight state

**Status:** Check `http://localhost:8080` for real-time dashboard, or `predd config` / `hunter status` for CLI info.

---

## Hunter Issue State Machine

```
new
 ↓
in_progress (proposal skill running)
 ↓
proposal_open (proposal PR created)
 ↓
implementing (proposal merged, impl skill running)
 ↓
self_reviewing (impl PR exists, reviewing it)
 ├→ implementing (issues found, fix loop, max N attempts)
 ↓
ready_for_review (review approved or loops exhausted)
 ↓
submitted (impl PR merged, issue closed)

failed (stuck state, can rollback to retry)
awaiting_verification (legacy, treated as terminal)
```

Labels applied:
- `{github_user}:in-progress` — claiming & proposal stage
- `{github_user}:proposal-open` — proposal PR open
- `{github_user}:implementing` — implementation in progress
- `{github_user}:hunter-failed` — stuck/failed (if `comment_on_failures=true`)

---

## Predd PR State Machine

```
submitted (review completed & posted)
reviewing (skill subprocess running)
rejected (skipped, already reviewed, own PR, draft)
failed (crash or no output)
awaiting_approval (legacy, not used in current version)
```

Labels applied (if `auto_label_prs=true`):
- `sdd-proposal` — PR is a proposal (created by hunter)
- `sdd-implementation` — PR is an implementation (created by hunter)

---

## Logs & Debugging

### State Files

- `~/.config/predd/state.json` — predd PR state
- `~/.config/predd/hunter-state.json` — hunter issue state

### Decision Logs

JSONL format, one record per event:

- `~/.config/predd/decisions.jsonl` — predd events
- `~/.config/predd/hunter-decisions.jsonl` — hunter events

Events include: `pr_review_started`, `pr_review_posted`, `pr_review_failed`, `issue_pickup`, `proposal_created`, `impl_created`, `issue_closed`, `rollback`, `failure_commented`, etc.

Query:

```bash
jq 'select(.event == "pr_review_failed")' ~/.config/predd/decisions.jsonl
jq 'select(.ts > "2026-05-13")' ~/.config/predd/decisions.jsonl
```

### Activity Logs

```bash
tail -f ~/.config/predd/log.txt         # predd activity
tail -f ~/.config/predd/hunter-log.txt  # hunter activity
```

Logs are rotated at 10MB (max 3 files each).

### Status Page JSON API

```bash
curl http://localhost:8080/api/status | jq
```

Returns:
- `timestamp` — last update time
- `predd.summary` — counts by status
- `predd.by_status` — PRs grouped by status
- `predd.recent_decisions` — last 10 decisions
- `hunter.*` — same structure for issues
- `recent_decisions` — merged activity log

---

## Troubleshooting

### Issue: Daemons not picking up config changes

**Solution:** Stop and restart the daemon:

```bash
tmux kill-session -t predd
tmux kill-session -t hunter
tmux new -s predd && predd start
tmux new -s hunter && hunter start
```

### Issue: "Review skill produced no output" errors

**Check:**
1. Skill file exists and is readable
2. Skill prompt is correct (task-first structure, not skill-first)
3. Logs show what the skill ran: `tail -f ~/.config/predd/log.txt`

**Fix:** Update skill and restart daemon.

### Issue: Git push failing ("branch protection")

**Check:**
1. `gh auth status` — confirm GitHub auth
2. Branch protection rules — allow your user to push to draft PRs
3. SSH/HTTPS credentials are current

**Fix:** Either relax branch protection or use SSH keys.

### Issue: AWS Bedrock authentication failing

**Check:**
1. `aws sts get-caller-identity` — confirm AWS creds
2. AWS profile exists: `aws configure list --profile <profile>`
3. Bedrock model is available in region: `aws bedrock list-foundation-models --region <region>`

**Fix:** Update AWS profile or region in config.

### Issue: High token spend on Bedrock

**Reason:** Bedrock charges per input+output tokens, no caching yet.

**Workaround:**
1. Set `max_new_issues_per_cycle = 1` to limit parallelism
2. Use cheaper model (Haiku vs Sonnet)
3. Reduce `poll_interval` if polling too frequently

Future: Bedrock prompt caching will reduce token spend by ~90%.

### Issue: Hunter stuck on "proposal_open" or "implementing"

**Check:** `hunter list | jq 'map(select(.status | contains("proposal_open")))'`

**Fix:** Manually merge the PR, or rollback and retry:

```bash
hunter rollback <issue>
```

---

## Development

### Testing

```bash
# Run tests
uv run --with pytest pytest test_pr_watcher.py test_hunter.py -q

# With coverage
uv run --with pytest --with pytest-cov pytest test_hunter.py --cov=hunter
```

Target: 80%+ coverage.

### Specs & Architecture

See `CLAUDE.md` for detailed architecture, design decisions, and conventions.

Implemented specs: `spec/archive/v0.0.1/`
Pending specs: `spec/changes/`

---

## License

MIT
