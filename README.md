# predd

Polls GitHub for open PRs, drafts reviews via Claude, waits for your approval before submitting.

## Prerequisites

- WSL2 with Python 3.12+ and `uv` installed
- `gh` CLI authenticated: `gh auth login`
- `claude` CLI authenticated: `claude login`
- `BurntToast` PowerShell module on Windows: `Install-Module BurntToast`
- `.wav` files for notification sounds (optional)

## Install

```bash
# Make executable and symlink to PATH
chmod +x /path/to/predd.py
ln -s /path/to/predd.py ~/.local/bin/predd
```

## 1. Configure repos

On first run, a config template is created automatically:

```bash
predd config
# → writes ~/.config/predd/config.toml and exits
```

Edit `~/.config/predd/config.toml`:

```toml
repos = [
  "owner/repo-one",
  "owner/repo-two",
]

github_user = "your-github-username"   # your PRs are skipped
worktree_base = "/home/you/pr-reviews" # where PR branches are checked out

poll_interval = 90                     # seconds between polls

# Optional: Windows-side .wav paths for sound alerts
sound_new_pr    = "C:\\Users\\you\\sounds\\new-pr.wav"
sound_review_ready = "C:\\Users\\you\\sounds\\review-ready.wav"

claude_model = "claude-opus-4-7"
```

Verify the resolved config loads cleanly:

```bash
predd config
```

## 2. Verify repo access

The daemon uses `gh` as a subprocess and inherits your existing auth — no extra setup needed.
Just confirm `gh` can already see the repo:

```bash
gh pr list --repo fusion-e/ai-bp-toolkit
```

If that works, the daemon will work.

## 3. Set up the daemon

Run in a `tmux` session (recommended):

```bash
tmux new -s predd
predd start
# Ctrl-B D to detach
```

Or with `nohup`:

```bash
nohup predd start >> ~/.config/predd/log.txt 2>&1 &
```

Or write a systemd user unit (`~/.config/systemd/user/predd.service`):

```ini
[Unit]
Description=predd daemon

[Service]
ExecStart=%h/.local/bin/predd start
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now predd
```

## 4. Verify it's working

**Single poll (no daemon needed):**

```bash
predd start --once
# Should exit cleanly; check log for any errors
```

**Check the log:**

```bash
tail -f ~/.config/predd/log.txt
```

**List pending reviews:**

```bash
predd list
```

**When a review is ready:**

```bash
predd show 123           # read the draft
# edit ~/.../review-draft.md if you want changes
predd approve 123        # submit as approval
predd comment 123        # submit as comment
predd request-changes 123 # submit as request-changes
predd reject 123         # discard (no GitHub submission)
```

PR argument accepts `owner/repo#123` or just `123` (unambiguous within your watched repos).

## Customize the review prompt

Edit `~/.config/predd/review-prompt.md` to change what Claude looks for.
The default prompt asks for a Verdict, Summary, Findings (with severity), and Questions for the author.
