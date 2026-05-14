# Config Wizard

## Problem

First-time setup requires manually editing `~/.config/predd/config.toml` after predd writes a blank template. The template contains placeholder values (`owner/repo`, `your-github-username`) with no guidance on what each field does or how to validate inputs. Users frequently misconfigure required fields, run `predd start --once`, and only discover problems at runtime when a subprocess fails.

Specific pain points:

1. **No field validation at write time.** A typo in `github_user` is not caught until a PR cycle runs and produces confusing log output.
2. **No GitHub auth check.** Users forget to run `gh auth login` before starting the daemon. `load_config()` doesn't verify `gh` is available.
3. **No Jira connectivity check.** `jira_base_url` can be set to an unreachable host. This is only discovered when the first CSV-based or API-based ingest runs.
4. **No path for adding repos later.** Adding a second repo means opening `config.toml` in an editor, remembering the `[[repo]]` syntax, and restarting the daemon.
5. **New `[[repo]]` schema (from per-repo-config.md) makes manual editing more error-prone** — array-of-tables TOML syntax is unfamiliar to many users.

## Solution

Add two commands to `predd`:

- `predd init` — interactive wizard that walks through all required and optional config fields, validates each, and writes `config.toml` atomically.
- `predd config` — read-only display of current config.
- `predd config set <key> <value>` — non-interactive one-off field update.

An optional `--ui` flag on `predd init` serves a local web form instead of the terminal wizard.

Neither command requires the daemon to be running. Both can be run independently of `predd start`.

## CLI Interface

### `predd init`

```
predd init [--force] [--ui] [--port PORT]
```

Behavior:

1. If `~/.config/predd/config.toml` already exists and `--force` is not given, print a warning and ask for confirmation before overwriting. Offer to start from the existing values (edit-in-place mode) rather than blank defaults.
2. Walk through fields in order (see Config Fields Reference below). For each field:
   - Print a one-line description of what the field controls.
   - Show the current/default value in brackets: `[default: devin]`.
   - Accept empty input to keep the current/default value.
   - Validate the input immediately; re-prompt on failure with an error message.
3. After all fields, run connectivity checks (see Validation Checks).
4. Write the config atomically (`config.toml.tmp` → `config.toml`).
5. Print a summary of the written config and suggest next steps (`predd start --once`).

Example session (abbreviated):

```
predd init

Welcome to predd setup. Press Enter to accept defaults.

GitHub user (your login, used to skip your own PRs)
  github_user []: adam

Worktree base directory (where git worktrees are created)
  worktree_base [~/worktrees]:

Backend to use for reviews and proposals
  backend (devin|claude) [devin]:

Checking GitHub auth... ok (adam)

Add a repo? (Enter owner/repo or leave blank to skip)
  repo: fusion-e/ai-bp-toolkit

  predd enabled for fusion-e/ai-bp-toolkit? [Y/n]:
  hunter enabled for fusion-e/ai-bp-toolkit? [Y/n]:
  obsidian enabled for fusion-e/ai-bp-toolkit? [y/N]:
  Jira CSV dir for fusion-e/ai-bp-toolkit (blank to skip): ~/jira/ai-bp-toolkit

Add another repo? [y/N]:

Config written to ~/.config/predd/config.toml
Next: predd start --once
```

### `predd config`

```
predd config
```

Prints the current config as a human-readable table (not raw TOML). Does not start the daemon. Exits non-zero if config file does not exist.

Output format (example):

```
github_user        adam
worktree_base      ~/worktrees
backend            devin
model              swe-1.6
trigger            ready
...

Repos:
  fusion-e/ai-bp-toolkit   predd=yes  hunter=yes  obsidian=no  jira_csv_dir=~/jira/ai-bp-toolkit
  fusion-e/other            predd=yes  hunter=no   obsidian=no  jira_csv_dir=(none)
```

### `predd config set <key> <value>`

```
predd config set <key> <value>
```

Updates a single top-level config field. Reads the existing config, modifies the target field, validates, and writes atomically.

Supported keys: all scalar top-level fields (`github_user`, `backend`, `model`, `trigger`, `worktree_base`, `branch_prefix`, `max_review_fix_loops`, `max_resume_retries`, `max_new_issues_per_cycle`, `orphan_scan_interval`, `auto_review_draft`, `auto_label_prs`, `collect_pr_feedback`, `jira_base_url`, `jira_api_enabled`, `jira_sprint_filter`).

Per-repo fields are not settable via `config set` — use `predd init --force` or edit `config.toml` directly.

Exits non-zero on unknown key or validation failure.

Example:

```
predd config set backend claude
predd config set max_review_fix_loops 3
```

### `hunter init`

Alias for `predd init`. Both tools share config, so this is a convenience entry point for users who think of themselves as "hunter users". Implemented by calling the same underlying function.

## Web UI (optional flag)

```
predd init --ui [--port 7331]
```

Starts a minimal HTTP server on `localhost:7331` (default), opens the browser, and serves a single-page HTML form pre-populated with current config values. On submit, validates fields server-side and writes the config.

Implementation constraints:

- No external dependencies. Use `http.server` (stdlib) + inline HTML/JS.
- Form layout mirrors the wizard field order.
- The `[[repo]]` section uses a dynamic "add repo" button (pure JS, no frameworks).
- Server shuts down automatically after a successful write (or on Ctrl-C).
- Not intended as a persistent management UI — one-shot config helper only.

The `--ui` path is lower priority than the terminal wizard. If skipped during initial implementation, leave the flag as a stub that prints "not yet implemented".

## Validation Checks

Run these after all fields are collected, before writing:

| Check | Command | Failure action |
|-------|---------|----------------|
| `gh` available | `gh --version` | Print install instructions, offer to skip |
| GitHub auth | `gh auth status` | Print `gh auth login` instructions, offer to skip |
| `worktree_base` exists | `os.path.isdir(expanduser(worktree_base))` | Offer to create it (`mkdir -p`) |
| Each `jira_csv_dir` | `os.path.isdir(expanduser(path))` | Offer to create it |
| Jira base URL (if set) | HTTP GET to `{jira_base_url}/rest/api/2/serverInfo` | Warn on failure, offer to skip — do not block write |
| Skill paths (if non-default) | `os.path.isfile(expanduser(path))` | Warn if missing |

All checks offer "skip" so the user is never hard-blocked. Skipped checks produce a warning at the end:

```
Warnings (skipped checks):
  - GitHub auth not verified. Run: gh auth login
  - worktree_base ~/worktrees does not exist (created)
```

## Implementation

### Location

All wizard logic lives in `predd.py` as a new `@cli.command("init")` Click command and a helper function `run_config_wizard(existing: Config | None, force: bool) -> Config`.

The `config` subcommand group (`predd config`, `predd config set`) is implemented as a Click group also in `predd.py`.

`hunter.py` exposes `hunter init` by importing and re-registering the same command from predd.

### Atomic Write

Use the same pattern as state files:

```python
tmp = config_path.with_suffix(".toml.tmp")
tmp.write_text(toml.dumps(cfg.to_dict()))
tmp.rename(config_path)
```

`Config.to_dict()` must emit the new `[[repo]]` schema (as specified in per-repo-config.md).

### No New Dependencies

Use `tomllib` (stdlib, Python 3.11+) for reading. For writing, either use a minimal inline TOML serializer or `tomli_w` (already acceptable since the project uses `uv` inline deps — add to the script header if needed).

Do not add `click` prompt machinery beyond what click already provides (`click.prompt`, `click.confirm`). Click is already a dependency.

## Config Fields Reference

Fields presented in wizard order:

**Required:**

| Field | Prompt | Validation |
|-------|--------|------------|
| `github_user` | "Your GitHub username" | Non-empty string, no spaces |
| `worktree_base` | "Directory where git worktrees are created" | Expandable path |
| `repos` / `[[repo]]` | Interactive repo-add loop | At least one repo required |

**Backend:**

| Field | Prompt | Default | Validation |
|-------|--------|---------|------------|
| `backend` | "Review backend" | `devin` | `devin` or `claude` |
| `model` | "Model name" | `swe-1.6` (devin) / `claude-opus-4-7` (claude) | Non-empty |

**Behaviour:**

| Field | Prompt | Default |
|-------|--------|---------|
| `trigger` | "Trigger mode (ready/requested)" | `ready` |
| `max_review_fix_loops` | "Max self-review fix loops" | `1` |
| `auto_review_draft` | "Review draft PRs?" | `false` |
| `max_resume_retries` | "Max resume retries before rollback" | `2` |
| `max_new_issues_per_cycle` | "Max new issues to pick up per repo per cycle" | `1` |
| `orphan_scan_interval` | "Orphan label scan interval (cycles)" | `10` |
| `auto_label_prs` | "Auto-label proposal/impl PRs?" | `true` |
| `collect_pr_feedback` | "Collect PR review feedback?" | `true` |
| `branch_prefix` | "Branch prefix for hunter-created branches" | `usr/at` |

**Skill paths** (shown in "advanced" section, skippable as a group):

| Field | Default |
|-------|---------|
| `skill_path` | `~/.windsurf/skills/pr-review/SKILL.md` |
| `proposal_skill_path` | `~/.windsurf/skills/proposal/SKILL.md` |
| `impl_skill_path` | `~/.windsurf/skills/impl/SKILL.md` |

**Jira** (shown only if user answers yes to "Configure Jira?"):

| Field | Prompt | Default |
|-------|--------|---------|
| `jira_base_url` | "Jira base URL" | — |
| `jira_api_enabled` | "Use Jira REST API?" | `false` |
| `jira_sprint_filter` | "Sprint filter (active/all/named:...)" | `active` |

**Per-repo fields** (collected in the repo-add loop):

| Field | Prompt | Default |
|-------|--------|---------|
| `predd` | "Enable predd for this repo?" | `true` |
| `hunter` | "Enable hunter for this repo?" | `true` |
| `obsidian` | "Enable obsidian for this repo?" | `false` |
| `jira_csv_dir` | "Jira CSV inbox dir (blank to skip)" | — |

## Testing

- `test_config_wizard_defaults`: run wizard with all-empty input, confirm defaults written correctly.
- `test_config_wizard_validation`: supply invalid `backend` value, confirm re-prompt; supply invalid path, confirm warning.
- `test_config_set_scalar`: `config set backend claude` updates only that field, leaves others unchanged.
- `test_config_set_unknown_key`: exits non-zero with clear error message.
- `test_config_show`: `predd config` with a known config file prints expected lines.
- `test_atomic_write`: simulates crash during write (mock `rename` raises), confirms original config unchanged.
- `test_wizard_edit_in_place`: existing config provided; all-empty input produces config identical to input.
- All tests mock filesystem and subprocess calls — no real disk writes or `gh` invocations.

## Out of Scope

- Per-repo skill path overrides (not yet in `RepoConfig` — see per-repo-config.md Out of Scope).
- Config schema migration (handled by `load_config` / `_load_repo_configs` per per-repo-config.md).
- A persistent web UI or config management dashboard.
- `predd config unset` — not needed; use `predd config set` with empty string or edit TOML directly.

## Files Touched

- `predd.py` — `init` command, `config` command group, `run_config_wizard`, `Config.to_dict()`
- `hunter.py` — `hunter init` alias registration
- `test_pr_watcher.py` — new wizard/config command tests
