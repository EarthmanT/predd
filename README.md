# predd

Two daemons that run in the background and do your GitHub work:

- **predd** — reviews every open PR assigned to you, posts inline comments via Claude
- **hunter** — picks up issues assigned to you, writes proposals, implements them, self-reviews

---

## Setup (tell Windsurf: "set this up for me")

> Clone this repo, then set up predd and hunter for my GitHub account. My username is `<your-github-username>`, my repos are `["owner/repo"]`, my worktrees go in `~/pr-reviews`, and my pr-review skill is at `<path-to-skill>`.

Windsurf will run the steps below.

---

## Manual setup

### 1. Prerequisites

- Python 3.12+ and `uv`
- `gh` CLI — run `gh auth login` once
- `claude` CLI — run `claude login` once (uses your subscription, no API key needed)

### 2. Install

```bash
git clone https://github.com/your-org/predd ~/windsurf/projects/predd
chmod +x ~/windsurf/projects/predd/predd.py ~/windsurf/projects/predd/hunter.py
ln -s ~/windsurf/projects/predd/predd.py ~/.local/bin/predd
ln -s ~/windsurf/projects/predd/hunter.py ~/.local/bin/hunter
```

### 3. Configure

```bash
predd start --once   # generates ~/.config/predd/config.toml then exits
```

Edit `~/.config/predd/config.toml`:

```toml
repos            = ["owner/repo"]          # watched by both predd and hunter
github_user      = "your-github-username"
worktree_base    = "/home/you/pr-reviews"

# Skills — point these at your actual skill files
skill_path          = "/path/to/pr-review/SKILL.md"
proposal_skill_path = "/path/to/sdd-proposal/SKILL.md"
impl_skill_path     = "/path/to/sdd-implementation/SKILL.md"

backend = "claude"
model   = "claude-haiku-4-5"
```

Verify:

```bash
predd config
gh pr list --repo owner/repo   # confirm gh auth works
```

### 4. Run

```bash
# predd — reviews PRs
tmux new -s predd
predd start
# Ctrl-B then d to detach

# hunter — picks up issues and writes proposals
tmux new -s hunter
hunter start
# Ctrl-B then d to detach
```

### 5. Verify

```bash
tail -f ~/.config/predd/log.txt         # predd activity
tail -f ~/.config/predd/hunter-log.txt  # hunter activity
predd list                               # pending reviews
hunter status                            # issue pipeline state
```

---

## Day-to-day

**predd** — runs silently, posts reviews on GitHub as they come in. Check `predd list` if you want to see what it has reviewed today.

**hunter** — when you're assigned an issue, hunter claims it, runs the proposal skill, opens a draft PR for you to review, then waits. Once you merge the proposal, it implements and self-reviews.

**Graceful shutdown:** `Ctrl-C` once waits for the current task to finish. `Ctrl-C` twice force-quits and rolls back.

---

## Trigger modes (predd)

```toml
trigger = "ready"      # review all open non-draft PRs (default)
trigger = "requested"  # only PRs where you are an explicit reviewer
```

## Backends

```toml
backend = "claude"   # uses claude CLI with your subscription
backend = "devin"    # uses devin CLI
```

## hunter config options

```toml
max_review_fix_loops = 1      # self-review cycles before flagging human
auto_review_draft    = false  # wait for PR to leave draft before self-reviewing
branch_prefix        = "usr/at"
max_resume_retries   = 2      # retries before rolling back a stuck issue
```
