# obsidian daemon

## What It Does

Third daemon alongside predd and hunter. Runs `obsidian observe` hourly and `obsidian analyze` daily. Self-contained — no state shared with predd/hunter beyond reading their outputs.

## Schedule

- **observe**: every hour (configurable via `observe_interval`)
- **analyze**: once daily at a configurable time (default: 08:00 local)

## CLI

```bash
obsidian start [--once]   # run one observe+analyze cycle then exit
obsidian observe          # run observe now
obsidian analyze          # run analyze now
```

## Run It

```bash
tmux new -s obsidian
obsidian start
# Ctrl-B d to detach
```

## Config

```toml
observe_interval = 3600       # seconds between observe runs
analyze_hour = 8              # hour of day to run analyze (local time)
analyze_days = 7              # days of observations to analyze
analyze_model = "claude-opus-4-7"
```

## PID / Log

- PID: `~/.config/predd/obsidian-pid`
- Log: `~/.config/predd/obsidian-log.txt`

## Implementation Notes

- Separate script: `obsidian.py` in the same repo, symlinked to `~/.local/bin/obsidian`
- Imports shared pieces from predd (Config, logging, `_run_claude`) the same way hunter does
- Graceful shutdown: same double-^C pattern
- `--once` skips PID file (safe for cron)

## The Self-Improvement Loop

```
obsidian observe (hourly)
  → reads hunter-state.json + decisions.jsonl
  → writes ~/.config/predd/obsidian/observations/*.md

obsidian analyze (daily)
  → reads observation notes
  → sends to Claude
  → writes spec/changes/*.md

hunter (next poll)
  → picks up new spec as a GH issue
  → writes proposal → you approve
  → implements → you review
  → merges → predd/hunter improve
```
