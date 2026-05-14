# Per-Repo Configuration

## Problem

Configuration today is global with per-daemon override lists:

```toml
repos = ["owner/a", "owner/b"]
predd_only_repos = []
hunter_only_repos = []
jira_csv_dir = "/single/path"
```

This forces three structural problems:

1. **One Jira CSV dir for all repos.** Multiple projects can't have separate CSV inboxes. Today, dropping `BPA-123` into the single inbox files it as an issue in *every* watched repo (with Jira-key dedup), which is rarely what you want — a Jira project usually maps to a specific GitHub repo.
2. **Per-daemon participation is awkward.** Three parallel lists (`repos`, `predd_only_repos`, `hunter_only_repos`) and no field for obsidian. Adding a fourth daemon means a fourth list.
3. **No room to grow.** Per-repo settings worth adding later (label prefixes, skill overrides, base branch defaults) have nowhere natural to live.

## Proposed Behaviour

Move to per-repo configuration via TOML array-of-tables. Backward-compatible: existing flat configs continue to work.

### New Schema

```toml
# Per-repo configuration (new schema)
[[repo]]
name = "fusion-e/ai-bp-toolkit"
predd = true
hunter = true
obsidian = true
jira_csv_dir = "~/windsurf/projects/predd/.jira/ai-bp-toolkit"

[[repo]]
name = "fusion-e/something-else"
predd = true
hunter = false
obsidian = false
jira_csv_dir = "~/windsurf/projects/predd/.jira/something-else"
```

Per-repo fields:

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | `owner/repo` GitHub slug |
| `predd` | bool | `true` | Whether predd watches this repo for PR reviews |
| `hunter` | bool | `true` | Whether hunter watches this repo for assigned issues |
| `obsidian` | bool | `true` | Whether obsidian includes this repo in observations/analysis |
| `jira_csv_dir` | string | `null` | Per-repo CSV inbox; if absent, no CSV ingest for this repo |

### Backward Compatibility

If a config file contains the old flat schema (`repos`, `predd_only_repos`, `hunter_only_repos`, or `jira_csv_dir` at top level), `load_config` synthesizes the new per-repo structure at load time:

- Every name in `repos` becomes a `[[repo]]` entry with `predd = true`, `hunter = true`, `obsidian = true`.
- Every name in `predd_only_repos` becomes `[[repo]]` with `predd = true`, `hunter = false`, `obsidian = false`.
- Every name in `hunter_only_repos` becomes `[[repo]]` with `predd = false`, `hunter = true`, `obsidian = false`.
- The global `jira_csv_dir` (if set) is applied to every synthesized entry.

If *both* schemas are present, the new schema wins and a warning is logged. (Don't error out — let people migrate incrementally.)

If the old `predd_only_repos` or `hunter_only_repos` lists are non-empty, log an INFO-level deprecation note pointing to the new schema.

### Config Class Changes

Add a `RepoConfig` dataclass and an accessor surface on `Config`:

```python
@dataclass
class RepoConfig:
    name: str
    predd: bool = True
    hunter: bool = True
    obsidian: bool = True
    jira_csv_dir: Path | None = None

class Config:
    def __init__(self, data: dict):
        # ... existing fields ...
        self.repo_configs: list[RepoConfig] = _load_repo_configs(data)
        # Keep cfg.repos / cfg.predd_only_repos / cfg.hunter_only_repos as
        # *derived* read-only views for any external code that still reads them.

    def repos_for(self, daemon: str) -> list[str]:
        """Return repo names where the given daemon is enabled.
        daemon: 'predd' | 'hunter' | 'obsidian'"""
        attr = daemon  # 'predd', 'hunter', 'obsidian'
        return [rc.name for rc in self.repo_configs if getattr(rc, attr)]

    def jira_csv_dir_for(self, repo: str) -> Path | None:
        for rc in self.repo_configs:
            if rc.name == repo:
                return rc.jira_csv_dir
        return None

    def repo_config(self, repo: str) -> RepoConfig | None:
        for rc in self.repo_configs:
            if rc.name == repo:
                return rc
        return None
```

The derived `cfg.repos`, `cfg.predd_only_repos`, `cfg.hunter_only_repos` properties stay for backward compat with code paths I haven't traced. They become read-only computed views from `repo_configs`.

`Config.to_dict()` emits the new schema. Old fields are omitted from output.

`_load_repo_configs` is the migration function — handles both schemas, normalizes to `list[RepoConfig]`, expanduser on paths.

### DEFAULT_CONFIG_TEMPLATE Update

Replace the existing repo / Jira blocks with:

```toml
# Per-repo configuration. One [[repo]] block per GitHub repo.
[[repo]]
name = "owner/repo"
predd = true
hunter = true
obsidian = true
# jira_csv_dir = "~/jira/owner-repo"
```

Comment out `jira_csv_dir` so users opt in explicitly per repo.

### Touch Sites

| File | Site | Change |
|---|---|---|
| `predd.py` | `start` poll loop, `for repo in cfg.repos:` | `for repo in cfg.repos_for("predd"):` |
| `hunter.py` | `start`, `hunter_repos = list(dict.fromkeys(cfg.repos + cfg.hunter_only_repos))` | `hunter_repos = cfg.repos_for("hunter")` |
| `hunter.py` | `ingest_jira_csv(cfg, repos)` | Iterate repos; for each, use `cfg.jira_csv_dir_for(repo)`; skip if `None`. The function no longer reads `cfg.jira_csv_dir` directly. Issue creation happens only in the repo whose CSV dir produced the row. |
| `obsidian.py` | `_build_observations` | Filter `hunter_state` entries and `predd_events` by `cfg.repos_for("obsidian")` before building observations. |
| `predd.py` | `get_status_json` / status page | Optionally filter shown repos to `cfg.repos_for("predd") + cfg.repos_for("hunter")` deduped. Not strictly necessary — state files already only contain watched repos. |

### Migration Path

Users migrate when they want. No forced rewrite. On daemon start with a flat config, a single INFO line:

```
config: using legacy flat schema (repos / *_only_repos). Migration to [[repo]] blocks recommended — see CLAUDE.md.
```

Document migration in `CLAUDE.md` with a worked example showing flat → per-repo for a two-repo setup.

## Out of Scope

- Per-repo skill paths (`skill_path`, `proposal_skill_path`, `impl_skill_path`). Worth adding later as `RepoConfig` fields with fallback to top-level config. Not in this spec — keep the change focused on participation flags + Jira dir.
- Per-repo backend / model selection. Same reason.
- Per-repo `branch_prefix`. Same reason.
- Auto-discovery of repos from `gh auth` or org listing. Manual config only.
- Removing the deprecated flat schema. Stays for at least one release.

## Acceptance Criteria

1. A config file using only the new `[[repo]]` schema loads cleanly and `predd config` shows it.
2. A config file using only the old flat schema loads cleanly, produces an INFO log line about legacy schema, and `cfg.repos_for("predd")` returns the expected list.
3. A config with both schemas present logs a warning and uses the new schema.
4. `cfg.repos_for("predd")`, `cfg.repos_for("hunter")`, `cfg.repos_for("obsidian")` each return only the repos with that flag true.
5. `cfg.jira_csv_dir_for("owner/a")` returns the per-repo path; for an unknown repo returns `None`.
6. With two repos configured, each with its own `jira_csv_dir`, dropping `BPA-1.csv` in repo A's dir creates an issue only in repo A (not in repo B).
7. Hunter only polls assigned issues from `cfg.repos_for("hunter")`.
8. Predd only polls PRs from `cfg.repos_for("predd")`.
9. Obsidian only includes observations for repos in `cfg.repos_for("obsidian")`.
10. New unit tests in `test_pr_watcher.py` cover: new-schema parsing, old-schema migration, both-schemas conflict, the three accessor methods, empty config edge case.
11. Existing tests pass.

## Files Touched

- `predd.py` — `RepoConfig`, `_load_repo_configs`, `Config` accessors, `DEFAULT_CONFIG_TEMPLATE`, poll loop, status page
- `hunter.py` — poll loop, `ingest_jira_csv` signature and body
- `obsidian.py` — `_build_observations` filter
- `test_pr_watcher.py` — new tests
- `CLAUDE.md` — schema documentation, migration example
- `README.md` — config reference table updates
