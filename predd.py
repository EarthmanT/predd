#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "click>=8.0",
#   "anthropic[bedrock]>=0.42.0",
# ]
# ///

import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

import click

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_DIR = Path("~/.config/predd").expanduser()
CONFIG_FILE = CONFIG_DIR / "config.toml"
STATE_FILE = CONFIG_DIR / "state.json"
HUNTER_STATE_FILE = CONFIG_DIR / "hunter-state.json"
PID_FILE = CONFIG_DIR / "pid"
LOG_FILE = CONFIG_DIR / "log.txt"
DECISION_LOG = CONFIG_DIR / "decisions.jsonl"
HUNTER_DECISION_LOG = CONFIG_DIR / "hunter-decisions.jsonl"

DEFAULT_REVIEW_PROMPT = """\
You are reviewing a pull request. Output a markdown document with this structure:

## Verdict
One of: APPROVE | COMMENT | REQUEST_CHANGES

## Summary
2-3 sentences on what this PR does.

## Findings
Bulleted list. Each finding: severity (blocker/concern/nit), file:line, description.
Skip anything that's just style preference. Focus on:
- Correctness bugs
- Edge cases not handled
- Missing or weak tests
- Anything that would page someone at 3am
- Security issues (auth, injection, secrets, etc.)

## Questions for the author
Anything ambiguous that needs clarification before merging.

Be direct. No praise sandwich. No ceremony.
"""

# Failure comment templates
_PREDD_NO_OUTPUT_COMMENT = """\
⚠️ Predd could not review this PR.

The AI review skill ran but produced no review output.

**PR:** {repo}#{pr_number}
**Error:** Review skill produced no output

Please either:
1. Review this PR manually
2. Check the skill prompt at ~/.windsurf/skills/pr-review/SKILL.md
3. Check logs: tail -f ~/.config/predd/log.txt
"""

_PREDD_CRASH_COMMENT = """\
⚠️ Predd crashed while reviewing this PR.

The AI review subprocess exited unexpectedly.

**PR:** {repo}#{pr_number}
**Error:** {error}

Please check the logs for details:
tail -f ~/.config/predd/log.txt
"""

DEFAULT_CONFIG_TEMPLATE = """\
# Per-repo configuration. One [[repo]] block per GitHub repo.
[[repo]]
name = "owner/repo"
predd = true
hunter = true
obsidian = true

# Polling interval in seconds
poll_interval = 90

# Sound alerts — built-in chimes (no .wav needed), or set a Windows-side .wav path
sound_new_pr = "new_pr"
sound_review_ready = "review_ready"

# Where to create git worktrees
worktree_base = "/home/<you>/pr-reviews"

# GitHub username (PRs authored by this user are skipped)
github_user = "<your-github-username>"

# Path to the pr-review skill (SKILL.md)
skill_path = "~/.windsurf/skills/pr-review/SKILL.md"

# Path to the proposal skill (SKILL.md)
proposal_skill_path = "~/.windsurf/skills/proposal/SKILL.md"

# Path to the implementation skill (SKILL.md)
impl_skill_path = "~/.windsurf/skills/impl/SKILL.md"

# When to trigger a review:
# "ready"     — any open non-draft PR (default)
# "requested" — only PRs where you are an explicit reviewer
trigger = "ready"

# Backend to use for reviews: "devin", "claude", or "bedrock"
backend = "bedrock"

# Model passed to the backend
# devin default: swe-1.6
# claude default: claude-opus-4-6
# bedrock default: eu.anthropic.claude-sonnet-4-5-20250929-v1:0
model = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"

# AWS Bedrock settings (when backend = "bedrock")
aws_profile = "default"
aws_region = "us-east-1"
bedrock_model = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"

# Branch prefix for hunter-created branches
branch_prefix = "usr/at"

# Maximum self-review/fix loops before flagging for human
max_review_fix_loops = 1

# Whether to self-review draft implementation PRs (false = wait for ready)
auto_review_draft = false

# Jira integration (optional)
# jira_base_url = "https://jira.cec.lab.emc.com"
# require_jira_conformance = true
# API integration: uncomment jira_api_enabled (token via JIRA_API_TOKEN env var)
# jira_api_enabled = false
# jira_projects = ["DAP09A"]  # Jira projects to ingest (defaults to DAP09A)
# Jira issue types to skip during ingest (case-insensitive)
skip_jira_issue_types = ["sub-task", "subtask", "sub task"]

# Sprint filter for Jira ingest.
# Options: "active" (default), "all", "named:<sprint-name>"
jira_sprint_filter = "active"

# Max new issues to pick up per repo per poll cycle
max_new_issues_per_cycle = 1

# How many poll cycles between orphaned-label scans (0 = startup only)
orphan_scan_interval = 10

# Auto-label unlabelled proposal/implementation PRs
auto_label_prs = true

# Collect review feedback from proposal/impl PRs and store in hunter state
collect_pr_feedback = true

# Status page server
status_server_enabled = true
status_port = 8080
status_refresh_interval = 30

# Obsidian daemon settings (tight test intervals, will loosen to 1hr/12hr once working)
observe_interval = 600        # seconds between observe runs (10 min for testing)
analyze_interval = 600        # seconds between analyze runs (10 min for testing)
fix_interval = 1200           # seconds between fix retry runs (20 min for testing)
analyze_days = 7              # days of observations to analyze
analyze_model = "claude-opus-4-7"

# Hour of the day (0-23, local time) to run obsidian analyze
analyze_hour = 9

# Failure commenting and cleanup
comment_on_failures = true
predd_failure_label = "{github_user}:predd-failed"
failure_cleanup_days = 7
failure_cleanup_interval = 10

# Post-CI review of hunter-created PRs (sentinel)
# post_ci_review_enabled = false
# post_ci_skill_path = "~/.windsurf/skills/post-ci-review/SKILL.md"
# max_open_auto_issues = 5
# auto_assign_filed_issues = true
"""

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("predd")
    logger.setLevel(logging.DEBUG)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(stderr_handler)
    return logger


logger = logging.getLogger("predd")

# ---------------------------------------------------------------------------
# Jira API Client
# ---------------------------------------------------------------------------

class JiraClient:
    """REST API client for Jira Server v2."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _make_request(self, method: str, endpoint: str, params: dict | None = None, max_retries: int = 3) -> dict:
        """Make HTTP request to Jira API with exponential backoff for rate limiting."""
        url = f"{self.base_url}/rest/api/2{endpoint}"
        if params:
            from urllib.parse import urlencode
            url += f"?{urlencode(params)}"

        req = urllib.request.Request(
            url,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            }
        )

        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # Check for rate limiting
                if e.code == 429:
                    retry_after = int(e.headers.get("Retry-After", 60))
                    if attempt < max_retries - 1:
                        wait_time = retry_after * (2 ** attempt)  # exponential backoff
                        logger.warning("Jira API rate limit, retrying in %ds (attempt %d/%d)", wait_time, attempt + 1, max_retries)
                        time.sleep(wait_time)
                        continue
                # Re-raise for non-rate-limit errors
                raise
            except urllib.error.URLError as e:
                # Network/connection errors — retry with backoff
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning("Jira API connection error: %s, retrying in %ds (attempt %d/%d)", e, wait_time, attempt + 1, max_retries)
                    time.sleep(wait_time)
                    continue
                raise

        raise RuntimeError(f"Jira API request failed after {max_retries} attempts")

    def search(self, jql: str, fields: list[str] | None = None, max_results: int = 50) -> list[dict]:
        """Run a JQL query and return issues."""
        if fields is None:
            fields = ["key", "summary", "status", "assignee"]

        params = {
            "jql": jql,
            "fields": ",".join(fields),
            "maxResults": str(max_results),
        }

        result = self._make_request("GET", "/search", params=params)
        return result.get("issues", [])

    def get_issue(self, key: str) -> dict:
        """Fetch a single issue by key."""
        return self._make_request("GET", f"/issue/{key}")

    def validate(self) -> bool:
        """Test auth via /rest/api/2/myself."""
        try:
            self._make_request("GET", "/myself")
            return True
        except Exception as e:
            logger.warning("Jira API validation failed: %s", e)
            return False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class RepoConfig:
    name: str
    predd: bool = True
    hunter: bool = True
    obsidian: bool = True


def _load_repo_configs(data: dict) -> list[RepoConfig]:
    """Parse repo configuration from TOML data, supporting both new [[repo]] and old flat schemas."""
    has_new = "repo" in data
    has_old = any(k in data for k in ("repos", "predd_only_repos", "hunter_only_repos"))

    if has_new and has_old:
        logger.warning(
            "config: both [[repo]] blocks and legacy repos/predd_only_repos/hunter_only_repos "
            "present — using new [[repo]] schema. Remove legacy keys to silence this warning."
        )

    if has_new:
        result = []
        for entry in data["repo"]:
            result.append(RepoConfig(
                name=entry["name"],
                predd=entry.get("predd", True),
                hunter=entry.get("hunter", True),
                obsidian=entry.get("obsidian", True),
            ))
        return result

    # Old flat schema — synthesize RepoConfig entries
    if has_old:
        deprecated = []
        if data.get("predd_only_repos"):
            deprecated.append("predd_only_repos")
        if data.get("hunter_only_repos"):
            deprecated.append("hunter_only_repos")
        if deprecated:
            logger.info(
                "config: using legacy flat schema (repos / %s). "
                "Migration to [[repo]] blocks recommended — see CLAUDE.md.",
                " / ".join(deprecated),
            )
        else:
            logger.info(
                "config: using legacy flat schema (repos / *_only_repos). "
                "Migration to [[repo]] blocks recommended — see CLAUDE.md."
            )

    result = []
    seen: set[str] = set()

    for name in data.get("repos", []):
        if name not in seen:
            seen.add(name)
            result.append(RepoConfig(
                name=name,
                predd=True,
                hunter=True,
                obsidian=True,
            ))

    for name in data.get("predd_only_repos", []):
        if name not in seen:
            seen.add(name)
            result.append(RepoConfig(
                name=name,
                predd=True,
                hunter=False,
                obsidian=False,
            ))

    for name in data.get("hunter_only_repos", []):
        if name not in seen:
            seen.add(name)
            result.append(RepoConfig(
                name=name,
                predd=False,
                hunter=True,
                obsidian=False,
            ))

    return result


class Config:
    def __init__(self, data: dict):
        self.repo_configs: list[RepoConfig] = _load_repo_configs(data)
        self.poll_interval: int = data.get("poll_interval", 90)
        self.sound_new_pr: str = data.get("sound_new_pr", "new_pr")
        self.sound_review_ready: str = data.get("sound_review_ready", "review_ready")
        self.worktree_base: Path = Path(data["worktree_base"])
        self.github_user: str = data["github_user"]
        self.skill_path: Path = Path(
            data.get(
                "skill_path",
                "~/.windsurf/skills/pr-review/SKILL.md",
            )
        ).expanduser()
        self.proposal_skill_path: Path = Path(
            data.get(
                "proposal_skill_path",
                "~/.windsurf/skills/proposal/SKILL.md",
            )
        ).expanduser()
        self.impl_skill_path: Path = Path(
            data.get(
                "impl_skill_path",
                "~/.windsurf/skills/impl/SKILL.md",
            )
        ).expanduser()
        self.trigger: str = data.get("trigger", "ready")
        self.backend: str = data.get("backend", "devin")
        # model: per-backend default; claude_model is accepted as legacy alias
        self.model: str = data.get("model") or data.get("claude_model") or (
            "swe-1.6" if data.get("backend", "devin") == "devin" else "claude-opus-4-7"
        )
        self.branch_prefix: str = data.get("branch_prefix", "usr/at")
        self.max_review_fix_loops: int = data.get("max_review_fix_loops", 1)
        self.auto_review_draft: bool = data.get("auto_review_draft", False)
        self.max_resume_retries: int = data.get("max_resume_retries", 2)
        # Max lines changed (additions + deletions) before skipping review and posting a comment.
        # Large diffs blow the model context window and rarely get useful reviews anyway.
        self.max_pr_diff_lines: int = data.get("max_pr_diff_lines", 2000)
        self.jira_base_url: str = data.get("jira_base_url", "https://jira.cec.lab.emc.com")
        self.jira_api_enabled: bool = data.get("jira_api_enabled", False)
        self.jira_projects: list[str] = data.get("jira_projects", ["DAP09A"])
        self.require_jira_conformance: bool = data.get("require_jira_conformance", True)
        self.max_new_issues_per_cycle: int = data.get("max_new_issues_per_cycle", 1)
        self.orphan_scan_interval: int = data.get("orphan_scan_interval", 10)
        self.auto_label_prs: bool = data.get("auto_label_prs", True)
        self.collect_pr_feedback: bool = data.get("collect_pr_feedback", True)
        self.status_server_enabled: bool = data.get("status_server_enabled", True)
        self.status_port: int = data.get("status_port", 8080)
        self.status_refresh_interval: int = data.get("status_refresh_interval", 30)
        self.observe_interval: int = data.get("observe_interval", 600)
        self.analyze_interval: int = data.get("analyze_interval", 600)
        self.fix_interval: int = data.get("fix_interval", 1200)
        self.analyze_days: int = data.get("analyze_days", 7)
        self.analyze_model: str = data.get("analyze_model", "claude-opus-4-7")
        self.predd_auto_post: bool = data.get("predd_auto_post", True)
        self.comment_on_failures: bool = data.get("comment_on_failures", True)
        self.predd_failure_label: str = data.get("predd_failure_label", "{github_user}:predd-failed")
        self.failure_cleanup_days: int = data.get("failure_cleanup_days", 7)
        self.failure_cleanup_interval: int = data.get("failure_cleanup_interval", 10)
        # Bedrock backend settings
        self.aws_profile: str = data.get("aws_profile", "default")
        self.aws_region: str = data.get("aws_region", "us-east-1")
        self.bedrock_model: str = data.get("bedrock_model", "eu.anthropic.claude-3-7-sonnet-20250219-v1:0")
        self.analyze_hour: int = data.get("analyze_hour", 9)
        self.skip_jira_issue_types: list[str] = data.get(
            "skip_jira_issue_types",
            ["sub-task", "subtask", "sub task"],
        )
        self.jira_sprint_filter: str = data.get("jira_sprint_filter", "active")
        self.jira_active_sprint_name: str = data.get("jira_active_sprint_name", "")
        # Validate jira_sprint_filter
        _jsf = self.jira_sprint_filter
        if _jsf not in ("active", "all") and not _jsf.startswith("named:"):
            logger.warning(
                "config: unrecognized jira_sprint_filter %r — falling back to 'active'",
                _jsf,
            )
            self.jira_sprint_filter = "active"
        # Post-CI review (sentinel)
        self.post_ci_review_enabled: bool = data.get("post_ci_review_enabled", False)
        self.post_ci_skill_path: Path = Path(
            data.get("post_ci_skill_path", "~/.windsurf/skills/post-ci-review/SKILL.md")
        ).expanduser()
        self.max_open_auto_issues: int = data.get("max_open_auto_issues", 5)
        self.auto_assign_filed_issues: bool = data.get("auto_assign_filed_issues", True)
        # Moonlight — respond to review comments on hunter-created PRs
        self.moonlight_enabled: bool = data.get("moonlight_enabled", True)
        self.max_moonlight_turns: int = data.get("max_moonlight_turns", 2)

    @property
    def repos(self) -> list[str]:
        """All repo names (predd + hunter + obsidian, deduped, order-preserving)."""
        seen: set[str] = set()
        result = []
        for rc in self.repo_configs:
            if rc.name not in seen:
                seen.add(rc.name)
                result.append(rc.name)
        return result

    @property
    def predd_only_repos(self) -> list[str]:
        """Repos where predd=True and hunter=False (backward compat view)."""
        return [rc.name for rc in self.repo_configs if rc.predd and not rc.hunter]

    @property
    def hunter_only_repos(self) -> list[str]:
        """Repos where hunter=True and predd=False (backward compat view)."""
        return [rc.name for rc in self.repo_configs if rc.hunter and not rc.predd]

    def repos_for(self, daemon: str) -> list[str]:
        """Return repo names where the given daemon is enabled.
        daemon: 'predd' | 'hunter' | 'obsidian'"""
        return [rc.name for rc in self.repo_configs if getattr(rc, daemon)]

    def repo_config(self, repo: str) -> RepoConfig | None:
        """Return the RepoConfig for a given repo slug, or None."""
        for rc in self.repo_configs:
            if rc.name == repo:
                return rc
        return None

    def to_dict(self) -> dict:
        """Serialize config to a dict suitable for TOML output.

        Uses the new [[repo]] schema (list of dicts under key "repo").
        """
        repo_list = []
        for rc in self.repo_configs:
            entry: dict = {
                "name": rc.name,
                "predd": rc.predd,
                "hunter": rc.hunter,
                "obsidian": rc.obsidian,
            }
            repo_list.append(entry)

        return {
            "github_user": self.github_user,
            "worktree_base": str(self.worktree_base),
            "backend": self.backend,
            "model": self.model,
            "trigger": self.trigger,
            "branch_prefix": self.branch_prefix,
            "max_review_fix_loops": self.max_review_fix_loops,
            "auto_review_draft": self.auto_review_draft,
            "max_resume_retries": self.max_resume_retries,
            "max_new_issues_per_cycle": self.max_new_issues_per_cycle,
            "orphan_scan_interval": self.orphan_scan_interval,
            "auto_label_prs": self.auto_label_prs,
            "collect_pr_feedback": self.collect_pr_feedback,
            "poll_interval": self.poll_interval,
            "sound_new_pr": self.sound_new_pr,
            "sound_review_ready": self.sound_review_ready,
            "skill_path": str(self.skill_path),
            "proposal_skill_path": str(self.proposal_skill_path),
            "impl_skill_path": str(self.impl_skill_path),
            "jira_base_url": self.jira_base_url,
            "jira_api_enabled": self.jira_api_enabled,
            "jira_projects": self.jira_projects,
            "require_jira_conformance": self.require_jira_conformance,
            "jira_sprint_filter": self.jira_sprint_filter,
            "skip_jira_issue_types": self.skip_jira_issue_types,
            "status_server_enabled": self.status_server_enabled,
            "status_port": self.status_port,
            "status_refresh_interval": self.status_refresh_interval,
            "observe_interval": self.observe_interval,
            "analyze_interval": self.analyze_interval,
            "fix_interval": self.fix_interval,
            "analyze_days": self.analyze_days,
            "analyze_model": self.analyze_model,
            "analyze_hour": self.analyze_hour,
            "predd_auto_post": self.predd_auto_post,
            "comment_on_failures": self.comment_on_failures,
            "predd_failure_label": self.predd_failure_label,
            "failure_cleanup_days": self.failure_cleanup_days,
            "failure_cleanup_interval": self.failure_cleanup_interval,
            "aws_profile": self.aws_profile,
            "aws_region": self.aws_region,
            "bedrock_model": self.bedrock_model,
            "post_ci_review_enabled": self.post_ci_review_enabled,
            "post_ci_skill_path": str(self.post_ci_skill_path),
            "max_open_auto_issues": self.max_open_auto_issues,
            "auto_assign_filed_issues": self.auto_assign_filed_issues,
            "repo": repo_list,
        }


def load_config() -> Config:
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(DEFAULT_CONFIG_TEMPLATE)
        review_prompt_path = CONFIG_DIR / "review-prompt.md"
        review_prompt_path.write_text(DEFAULT_REVIEW_PROMPT)
        click.echo(f"Created default config at {CONFIG_FILE}")
        click.echo("Edit it to add your repos and settings, then re-run.")
        sys.exit(0)

    with open(CONFIG_FILE, "rb") as f:
        data = tomllib.load(f)
    return Config(data)


# ---------------------------------------------------------------------------
# Config TOML serializer
# ---------------------------------------------------------------------------

def _toml_value(v) -> str:
    """Render a Python value as a TOML value string."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, list):
        items = ", ".join(_toml_value(x) for x in v)
        return f"[{items}]"
    return f'"{v}"'


def _serialize_config_toml(d: dict) -> str:
    """Serialize a config dict to TOML text.

    Handles scalar fields and the special 'repo' list-of-dicts key,
    which becomes [[repo]] array-of-tables blocks.
    """
    lines = []
    repo_list = d.pop("repo", [])

    for k, v in d.items():
        if v is None:
            continue
        lines.append(f"{k} = {_toml_value(v)}")

    for repo_entry in repo_list:
        lines.append("")
        lines.append("[[repo]]")
        for rk, rv in repo_entry.items():
            lines.append(f"{rk} = {_toml_value(rv)}")

    return "\n".join(lines) + "\n"


def _write_config_atomic(cfg_dict: dict, path: Path | None = None) -> None:
    """Write config dict to path atomically via a .tmp file."""
    if path is None:
        path = CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(_serialize_config_toml(cfg_dict))
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Config wizard
# ---------------------------------------------------------------------------

_WIZARD_SCALAR_KEYS = [
    "github_user", "worktree_base", "backend", "model", "trigger",
    "max_review_fix_loops", "auto_review_draft", "max_resume_retries",
    "max_new_issues_per_cycle", "orphan_scan_interval", "auto_label_prs",
    "collect_pr_feedback", "branch_prefix", "jira_base_url",
    "jira_api_enabled", "jira_sprint_filter",
]


def run_config_wizard(force: bool = False) -> None:
    """Interactive config wizard. Writes ~/.config/predd/config.toml."""
    existing: Config | None = None

    if CONFIG_FILE.exists() and not force:
        click.echo(f"Config already exists at {CONFIG_FILE}")
        overwrite = click.confirm("Overwrite from scratch?", default=False)
        if not overwrite:
            click.echo("Starting in edit-in-place mode (existing values shown as defaults).")
            try:
                with open(CONFIG_FILE, "rb") as f:
                    _data = tomllib.load(f)
                existing = Config(_data)
            except Exception as e:
                click.echo(f"Warning: could not load existing config ({e}). Using defaults.")

    click.echo("\nWelcome to predd setup. Press Enter to accept defaults.\n")

    # --- Required fields ---
    def _d(field: str, fallback):
        """Return existing value if available, else fallback."""
        if existing is not None:
            val = getattr(existing, field, fallback)
            if isinstance(val, Path):
                return str(val)
            return val
        return fallback

    github_user = click.prompt("GitHub username (used to skip your own PRs)", default=_d("github_user", ""))
    if not github_user or " " in github_user:
        click.echo("Error: github_user must be a non-empty string with no spaces.")
        raise SystemExit(1)

    worktree_base = click.prompt(
        "Worktree base directory (where git worktrees are created)",
        default=_d("worktree_base", "~/worktrees"),
    )

    # --- Backend ---
    backend_default = _d("backend", "devin")
    backend = click.prompt("Backend (devin|claude|bedrock)", default=backend_default)
    while backend not in ("devin", "claude", "bedrock"):
        click.echo("Error: backend must be one of: devin, claude, bedrock")
        backend = click.prompt("Backend (devin|claude|bedrock)", default=backend_default)

    model_defaults = {"devin": "swe-1.6", "claude": "claude-opus-4-7", "bedrock": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"}
    model = click.prompt("Model name", default=_d("model", model_defaults.get(backend, "swe-1.6")))

    # --- Advanced / behaviour ---
    advanced = click.confirm("\nConfigure advanced settings?", default=False)
    trigger = _d("trigger", "ready")
    max_review_fix_loops = _d("max_review_fix_loops", 1)
    auto_review_draft = _d("auto_review_draft", False)
    max_resume_retries = _d("max_resume_retries", 2)
    max_new_issues_per_cycle = _d("max_new_issues_per_cycle", 1)
    orphan_scan_interval = _d("orphan_scan_interval", 10)
    auto_label_prs = _d("auto_label_prs", True)
    collect_pr_feedback = _d("collect_pr_feedback", True)
    branch_prefix = _d("branch_prefix", "usr/at")

    if advanced:
        trigger_val = click.prompt("Trigger mode (ready/requested)", default=trigger)
        while trigger_val not in ("ready", "requested"):
            click.echo("Error: trigger must be 'ready' or 'requested'")
            trigger_val = click.prompt("Trigger mode (ready/requested)", default=trigger)
        trigger = trigger_val
        max_review_fix_loops = click.prompt("Max self-review fix loops", default=max_review_fix_loops, type=int)
        auto_review_draft = click.confirm("Review draft PRs?", default=auto_review_draft)
        max_resume_retries = click.prompt("Max resume retries before rollback", default=max_resume_retries, type=int)
        max_new_issues_per_cycle = click.prompt("Max new issues per repo per cycle", default=max_new_issues_per_cycle, type=int)
        orphan_scan_interval = click.prompt("Orphan label scan interval (cycles, 0=startup only)", default=orphan_scan_interval, type=int)
        auto_label_prs = click.confirm("Auto-label proposal/impl PRs?", default=auto_label_prs)
        collect_pr_feedback = click.confirm("Collect PR review feedback?", default=collect_pr_feedback)
        branch_prefix = click.prompt("Branch prefix for hunter-created branches", default=branch_prefix)

    # --- Jira ---
    jira_base_url = _d("jira_base_url", "")
    jira_api_enabled = _d("jira_api_enabled", False)
    jira_sprint_filter = _d("jira_sprint_filter", "active")

    configure_jira = click.confirm("\nConfigure Jira integration?", default=bool(jira_base_url and jira_base_url != "https://jira.cec.lab.emc.com"))
    if configure_jira:
        jira_base_url = click.prompt("Jira base URL", default=jira_base_url or "https://jira.example.com")
        jira_api_enabled = click.confirm("Use Jira REST API?", default=jira_api_enabled)
        jira_sprint_filter = click.prompt("Sprint filter (active/all/named:...)", default=jira_sprint_filter)

    # --- Repos ---
    click.echo("")
    repo_configs: list[RepoConfig] = []
    if existing and existing.repo_configs:
        click.echo("Existing repos:")
        for rc in existing.repo_configs:
            click.echo(f"  {rc.name}  predd={rc.predd}  hunter={rc.hunter}  obsidian={rc.obsidian}")
        keep_existing = click.confirm("Keep existing repos?", default=True)
        if keep_existing:
            repo_configs = list(existing.repo_configs)

    while True:
        prompt_text = "Add another repo? (owner/repo or blank to finish)" if repo_configs else "Add a repo (owner/repo)"
        repo_name = click.prompt(prompt_text, default="")
        if not repo_name:
            if not repo_configs:
                click.echo("At least one repo is required.")
                continue
            break
        if "/" not in repo_name:
            click.echo("Error: repo must be in owner/repo format.")
            continue
        predd_enabled = click.confirm(f"  predd enabled for {repo_name}?", default=True)
        hunter_enabled = click.confirm(f"  hunter enabled for {repo_name}?", default=True)
        obsidian_enabled = click.confirm(f"  obsidian enabled for {repo_name}?", default=False)
        repo_configs.append(RepoConfig(
            name=repo_name,
            predd=predd_enabled,
            hunter=hunter_enabled,
            obsidian=obsidian_enabled,
        ))

    # --- Validation checks ---
    click.echo("\nRunning validation checks...")
    warnings: list[str] = []

    # Check gh available
    try:
        subprocess.run(["gh", "--version"], capture_output=True, check=True)
        click.echo("  gh available: ok")
    except (subprocess.CalledProcessError, FileNotFoundError):
        click.echo("  Warning: 'gh' not found. Install from https://cli.github.com/")
        warnings.append("'gh' not found. Install from https://cli.github.com/")

    # Check gh auth
    try:
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        if result.returncode == 0:
            click.echo("  GitHub auth: ok")
        else:
            click.echo("  Warning: not authenticated with GitHub. Run: gh auth login")
            warnings.append("GitHub auth not verified. Run: gh auth login")
    except FileNotFoundError:
        pass  # already warned about gh not found

    # Check worktree_base
    wb_path = Path(worktree_base).expanduser()
    if wb_path.is_dir():
        click.echo(f"  worktree_base {worktree_base}: ok")
    else:
        create_wb = click.confirm(f"  worktree_base {worktree_base} does not exist. Create it?", default=True)
        if create_wb:
            wb_path.mkdir(parents=True, exist_ok=True)
            click.echo(f"  Created {worktree_base}")
            warnings.append(f"worktree_base {worktree_base} did not exist (created)")
        else:
            warnings.append(f"worktree_base {worktree_base} does not exist")

    # --- Build config dict and write ---
    cfg_dict: dict = {
        "github_user": github_user,
        "worktree_base": worktree_base,
        "backend": backend,
        "model": model,
        "trigger": trigger,
        "max_review_fix_loops": max_review_fix_loops,
        "auto_review_draft": auto_review_draft,
        "max_resume_retries": max_resume_retries,
        "max_new_issues_per_cycle": max_new_issues_per_cycle,
        "orphan_scan_interval": orphan_scan_interval,
        "auto_label_prs": auto_label_prs,
        "collect_pr_feedback": collect_pr_feedback,
        "branch_prefix": branch_prefix,
    }
    if jira_base_url:
        cfg_dict["jira_base_url"] = jira_base_url
    if jira_api_enabled:
        cfg_dict["jira_api_enabled"] = jira_api_enabled
    cfg_dict["jira_sprint_filter"] = jira_sprint_filter

    repo_list = []
    for rc in repo_configs:
        entry: dict = {
            "name": rc.name,
            "predd": rc.predd,
            "hunter": rc.hunter,
            "obsidian": rc.obsidian,
        }
        repo_list.append(entry)
    cfg_dict["repo"] = repo_list

    _write_config_atomic(cfg_dict)
    click.echo(f"\nConfig written to {CONFIG_FILE}")

    if warnings:
        click.echo("\nWarnings:")
        for w in warnings:
            click.echo(f"  - {w}")

    click.echo("\nNext steps: predd start --once")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to read state file; starting fresh")
        return {}


def save_state(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_FILE)


def update_pr_state(state: dict, key: str, **fields) -> None:
    if key not in state:
        state[key] = {}
    state[key].update(fields)
    save_state(state)

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

# Two-note chime: mid C then high E — audible but not jarring
_CHIME_NEW_PR = "[Console]::Beep(523,180); Start-Sleep -Milliseconds 80; [Console]::Beep(659,350)"
# Three-note ascending chime: C E G — signals something is ready
_CHIME_REVIEW_READY = "[Console]::Beep(523,150); Start-Sleep -Milliseconds 60; [Console]::Beep(659,150); Start-Sleep -Milliseconds 60; [Console]::Beep(784,400)"


_PWSH = shutil.which("pwsh.exe")


def notify_sound(sound_path: str) -> None:
    if not sound_path or not _PWSH:
        return
    if sound_path in ("new_pr", "review_ready"):
        beep_cmd = _CHIME_NEW_PR if sound_path == "new_pr" else _CHIME_REVIEW_READY
        try:
            subprocess.run(
                [_PWSH, "-NoProfile", "-Command", beep_cmd],
                check=False, capture_output=True, timeout=10,
            )
        except Exception:
            pass
    else:
        # Pass .wav path out-of-band via -ArgumentList to avoid injection
        try:
            subprocess.run(
                [_PWSH, "-NoProfile", "-Command",
                 "param($p); (New-Object Media.SoundPlayer $p).PlaySync()",
                 "-ArgumentList", sound_path],
                check=False, capture_output=True, timeout=10,
            )
        except Exception:
            pass


def notify_toast(title: str, body: str) -> None:
    if not _PWSH:
        return
    try:
        subprocess.run(
            [_PWSH, "-NoProfile", "-Command",
             "param($t,$b); New-BurntToastNotification -Text $t,$b",
             "-ArgumentList", title, body],
            check=False, capture_output=True, timeout=15,
        )
    except Exception:
        pass

# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

_TRANSIENT_ERRORS = ("rate limit", "502", "503", "504", "timeout", "connection")
_PERMANENT_ERRORS = ("not found", "404", "401", "403", "unauthorized",
                     "forbidden", "422", "unprocessable", "already exists")


def gh_run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = None
    for attempt in range(3):
        result = subprocess.run(["gh"] + args, capture_output=True, text=True)
        if result.returncode == 0 or not check:
            return result
        stderr = result.stderr.lower()
        if any(x in stderr for x in _PERMANENT_ERRORS):
            # Permanent failure — don't retry
            result.check_returncode()
        elif any(x in stderr for x in _TRANSIENT_ERRORS):
            wait = 2 ** attempt * 5
            logger.warning("gh transient error (attempt %d), retrying in %ds: %s",
                           attempt + 1, wait, result.stderr.strip())
            time.sleep(wait)
        else:
            # Unknown error — don't retry
            result.check_returncode()
    if check:
        result.check_returncode()
    return result


def gh_list_open_prs(repo: str) -> list[dict]:
    """Return list of open, non-draft PRs not authored by the current user."""
    result = gh_run([
        "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number,title,author,headRefName,headRefOid,baseRefName,isDraft,reviewRequests",
        "--limit", "100",
    ])
    prs = json.loads(result.stdout)
    return prs


def gh_pr_view(repo: str, pr_number: int) -> str:
    result = gh_run(["pr", "view", str(pr_number), "--repo", repo])
    return result.stdout


def gh_pr_already_reviewed(repo: str, pr_number: int, github_user: str) -> bool:
    """Return True if the PR is closed/merged or github_user has already reviewed it."""
    result = gh_run([
        "pr", "view", str(pr_number), "--repo", repo,
        "--json", "state,reviews",
    ])
    data = json.loads(result.stdout)
    if data.get("state", "OPEN") != "OPEN":
        return True
    return any(
        r.get("author", {}).get("login") == github_user
        for r in data.get("reviews", [])
    )


def gh_pr_diff(repo: str, pr_number: int) -> str:
    result = gh_run(["pr", "diff", str(pr_number), "--repo", repo])
    return result.stdout


def gh_pr_review(repo: str, pr_number: int, review_type: str, body_file: Path) -> None:
    flag_map = {
        "approve": "--approve",
        "comment": "--comment",
        "request-changes": "--request-changes",
    }
    flag = flag_map[review_type]
    gh_run([
        "pr", "review", str(pr_number),
        "--repo", repo,
        flag,
        "--body-file", str(body_file),
    ])


def gh_repo_default_branch(repo: str) -> str:
    """Return the default branch name for a repo."""
    result = gh_run(["repo", "view", repo, "--json", "defaultBranchRef"])
    data = json.loads(result.stdout)
    name = (data.get("defaultBranchRef") or {}).get("name")
    if not name:
        raise RuntimeError(f"Could not determine default branch for {repo}")
    return name


def gh_pr_comment(repo: str, pr_number: int, body: str) -> None:
    """Post a comment on a PR."""
    gh_run([
        "pr", "comment", str(pr_number),
        "--repo", repo,
        "--body", body,
    ])


def gh_issue_comment(repo: str, issue_number: int, body: str) -> None:
    """Post a comment on an issue."""
    gh_run([
        "issue", "comment", str(issue_number),
        "--repo", repo,
        "--body", body,
    ])


def gh_pr_add_label(repo: str, pr_number: int, label: str) -> None:
    """Add a label to a PR."""
    try:
        gh_run([
            "pr", "edit", str(pr_number),
            "--repo", repo,
            "--add-label", label,
        ])
    except Exception:
        pass  # Label already exists or other issue, don't crash


def gh_issue_add_label(repo: str, issue_number: int, label: str) -> None:
    """Add a label to an issue."""
    try:
        gh_run([
            "issue", "edit", str(issue_number),
            "--repo", repo,
            "--add-label", label,
        ])
    except Exception:
        pass  # Label already exists or other issue, don't crash

# ---------------------------------------------------------------------------
# Worktree helpers
# ---------------------------------------------------------------------------

def repo_slug(repo: str) -> str:
    return repo.replace("/", "-")


def worktree_path(cfg: Config, repo: str, pr_number: int, head_sha: str) -> Path:
    return cfg.worktree_base / f"{repo_slug(repo)}-{pr_number}-{head_sha[:7]}"


def find_local_repo(repo: str) -> Path | None:
    """Try to find a local clone of the repo by checking common locations."""
    home = Path.home()
    repo_name = repo.split("/")[1]
    candidates = [
        home / repo_name,
        home / "src" / repo_name,
        home / "repos" / repo_name,
        home / "projects" / repo_name,
        home / "code" / repo_name,
        home / "windsurf" / "projects" / repo_name,
        home / "windsurf" / repo_name,
    ]
    for candidate in candidates:
        if (candidate / ".git").exists():
            try:
                result = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    capture_output=True, text=True, check=True, cwd=candidate
                )
                if f"/{repo_name}" in result.stdout or f":{repo_name}" in result.stdout:
                    return candidate
            except subprocess.CalledProcessError:
                pass
    return None


def _worktree_cleanup(local_repo: Path, wt_path: Path, branch: str | None = None) -> None:
    """Remove stale worktree registration and optionally delete the local branch."""
    # Prune first — catches directories already deleted manually
    subprocess.run(["git", "worktree", "prune"], cwd=local_repo, capture_output=True)
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_path)],
        cwd=local_repo, capture_output=True,
    )
    # Prune again — catches anything remove left behind
    subprocess.run(["git", "worktree", "prune"], cwd=local_repo, capture_output=True)
    if branch:
        subprocess.run(["git", "branch", "-D", branch], cwd=local_repo, capture_output=True)


def setup_worktree(cfg: Config, repo: str, pr_number: int, head_sha: str, head_ref: str) -> Path:
    """Check out an existing PR branch into a new worktree."""
    wt_path = worktree_path(cfg, repo, pr_number, head_sha)
    cfg.worktree_base.mkdir(parents=True, exist_ok=True)

    local_repo = find_local_repo(repo)
    if local_repo:
        subprocess.run(
            ["git", "fetch", "origin", head_ref],
            cwd=local_repo, check=True, capture_output=True,
        )
        _worktree_cleanup(local_repo, wt_path)
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), f"origin/{head_ref}"],
            cwd=local_repo, check=True, capture_output=True,
        )
    else:
        tmp_dir = cfg.worktree_base / f"_tmp-{repo_slug(repo)}-{pr_number}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        if wt_path.exists():
            shutil.rmtree(wt_path)
        tmp_dir.mkdir(parents=True)
        subprocess.run(
            ["gh", "repo", "clone", repo, str(tmp_dir)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["gh", "pr", "checkout", str(pr_number)],
            cwd=tmp_dir, check=True, capture_output=True,
        )
        tmp_dir.rename(wt_path)

    return wt_path


def setup_new_branch_worktree(cfg: Config, repo: str, branch: str, base_branch: str) -> Path:
    """Create a new branch and worktree for proposal/impl work."""
    wt_path = cfg.worktree_base / f"{repo_slug(repo)}-{branch.replace('/', '-')}"
    cfg.worktree_base.mkdir(parents=True, exist_ok=True)

    local_repo = find_local_repo(repo)
    if local_repo:
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=local_repo, check=True, capture_output=True,
        )
        _worktree_cleanup(local_repo, wt_path, branch=branch)
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(wt_path), f"origin/{base_branch}"],
            cwd=local_repo, check=True, capture_output=True,
        )
    else:
        if wt_path.exists():
            shutil.rmtree(wt_path)
        wt_path.mkdir(parents=True)
        subprocess.run(
            ["gh", "repo", "clone", repo, str(wt_path)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "-b", branch],
            cwd=str(wt_path), check=True, capture_output=True,
        )

    return wt_path


def remove_worktree(worktree: str | Path) -> None:
    wt = Path(worktree)
    # Find the git dir to run worktree remove from
    try:
        # Walk up to find parent repo
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=wt, capture_output=True, text=True, check=True,
        )
        git_common_raw = result.stdout.strip()
        if os.path.isabs(git_common_raw):
            git_common = Path(git_common_raw)
        else:
            git_common = (wt / git_common_raw).resolve()
        repo_root = git_common.parent if git_common.name == ".git" else git_common.parent.parent
        subprocess.run(
            ["git", "worktree", "remove", str(wt), "--force"],
            cwd=repo_root, check=True, capture_output=True,
        )
    except Exception:
        # If we can't use git worktree remove, just delete the directory
        shutil.rmtree(wt, ignore_errors=True)

# ---------------------------------------------------------------------------
# Review pipeline
# ---------------------------------------------------------------------------

_DEVIN_STRIP_ENV = {
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
}


_INLINE_COMMENT_KEYWORDS = ["gh pr review", "inline", "file:line", "line comment", "start-line"]

def _load_skill(cfg: Config, pr_number: int) -> str:
    if not cfg.skill_path.exists():
        raise FileNotFoundError(f"Skill not found: {cfg.skill_path}")
    skill_body = cfg.skill_path.read_text()
    if not any(kw in skill_body.lower() for kw in _INLINE_COMMENT_KEYWORDS):
        logger.warning(
            "Review skill at %s may not post inline comments. "
            "Consider adding 'gh pr review' or inline comment instructions.",
            cfg.skill_path,
        )
    return skill_body.replace("$ARGUMENTS", str(pr_number))


# Active subprocess handle — set while a review is running so the shutdown
# handler can terminate it on a second signal.
_active_proc: subprocess.Popen | None = None
_stop = threading.Event()
_current_pr_key: list[str] = []  # mutable container so inner functions can write it


def _shutdown(signum, frame):
    if _stop.is_set():
        # Second signal — force quit
        key = _current_pr_key[0] if _current_pr_key else None
        if _active_proc is not None:
            logger.warning("Force quit — killing review subprocess")
            _active_proc.terminate()
        if key:
            logger.warning("Force quit — rolling back %s to unprocessed", key)
            state = load_state()
            state.pop(key, None)
            save_state(state)
        release_pid_file()
        sys.exit(1)
    _stop.set()
    if _active_proc is not None:
        logger.info("Finishing current review before exiting (^C again to force quit)...")


def _run_proc(cmd: list[str], worktree: Path, env: dict | None = None,
              stdin_text: str | None = None) -> str:
    global _active_proc
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        text=True,
        cwd=str(worktree),
        env=env,
    )
    _active_proc = proc
    try:
        stdout, stderr_out = proc.communicate(input=stdin_text, timeout=900)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        _active_proc = None
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr_out)
    return stdout


def _run_claude(cfg: Config, prompt: str, worktree: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    return _run_proc(
        ["claude", "-p", "--dangerously-skip-permissions", "--model", cfg.model],
        worktree,
        env=env,
        stdin_text=prompt,
    )


def _run_devin(cfg: Config, prompt: str, worktree: Path) -> str:
    skill_dir = worktree / ".devin" / "skills"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / (cfg.skill_path.stem.lower() + ".md")).write_text(cfg.skill_path.read_text())
    env = {k: v for k, v in os.environ.items() if k not in _DEVIN_STRIP_ENV}
    return _run_proc(
        ["setsid", "devin", "-p", "--permission-mode", "auto",
         "--model", cfg.model, "--", prompt],
        worktree,
        env=env,
    )


def run_review(cfg: Config, repo: str, pr_number: int, worktree: Path) -> str:
    prompt = _load_skill(cfg, pr_number)
    if cfg.backend == "claude":
        return _run_claude(cfg, prompt, worktree)
    if cfg.backend == "devin":
        return _run_devin(cfg, prompt, worktree)
    if cfg.backend == "bedrock":
        return _run_bedrock_skill(cfg, prompt, cfg.skill_path, worktree)
    raise ValueError(f"Unknown backend '{cfg.backend}'. Valid values: claude, devin, bedrock")

# ---------------------------------------------------------------------------
# Moonlight — fix hunter PRs in response to review comments
# ---------------------------------------------------------------------------

def _run_skill_prompt(cfg: "Config", prompt: str, worktree: Path) -> str:
    """Run a free-form prompt through the configured backend. Returns output."""
    if cfg.backend == "claude":
        return _run_claude(cfg, prompt, worktree)
    if cfg.backend == "devin":
        return _run_devin(cfg, prompt, worktree)
    if cfg.backend == "bedrock":
        return _run_bedrock_skill(cfg, prompt, cfg.impl_skill_path, worktree)
    raise ValueError(f"Unknown backend '{cfg.backend}'")


def _fetch_pr_review_comments(repo: str, pr_number: int) -> list[dict]:
    """Return all reviews + inline comments for a PR via gh api."""
    comments = []

    # Top-level reviews (REQUEST_CHANGES, APPROVE, COMMENT with body)
    try:
        result = gh_run(
            ["api", f"repos/{repo}/pulls/{pr_number}/reviews",
             "--paginate", "--jq", ".[]"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                try:
                    obj = json.loads(line)
                    if obj.get("body", "").strip() or obj.get("state") == "CHANGES_REQUESTED":
                        comments.append({
                            "id": obj.get("id"),
                            "type": "review",
                            "state": obj.get("state"),
                            "author": (obj.get("user") or {}).get("login", ""),
                            "body": obj.get("body", "").strip(),
                            "submitted_at": obj.get("submitted_at", ""),
                        })
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        logger.debug("Could not fetch reviews for PR #%d: %s", pr_number, e)

    # Inline review comments
    try:
        result = gh_run(
            ["api", f"repos/{repo}/pulls/{pr_number}/comments",
             "--paginate", "--jq", ".[]"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                try:
                    obj = json.loads(line)
                    comments.append({
                        "id": obj.get("id"),
                        "type": "inline",
                        "author": (obj.get("user") or {}).get("login", ""),
                        "path": obj.get("path", ""),
                        "line": obj.get("line") or obj.get("original_line"),
                        "body": obj.get("body", "").strip(),
                        "submitted_at": obj.get("created_at", ""),
                    })
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        logger.debug("Could not fetch inline comments for PR #%d: %s", pr_number, e)

    return comments


def _build_moonlight_prompt(repo: str, pr_number: int, branch: str,
                             worktree: Path, comments: list[dict]) -> str:
    lines = [
        f"You are fixing review comments on PR #{pr_number} in {repo}.",
        f"Branch: {branch}",
        f"Workspace: {worktree}",
        "",
        "## Review comments to address",
        "",
    ]
    for c in comments:
        author = c.get("author", "reviewer")
        body = c.get("body", "").strip()
        if not body:
            continue
        if c["type"] == "inline":
            path = c.get("path", "")
            line = c.get("line")
            loc = f"{path}:{line}" if line else path
            lines.append(f"**{author}** on `{loc}`:")
        else:
            state = c.get("state", "")
            label = f" ({state})" if state and state != "COMMENTED" else ""
            lines.append(f"**{author}**{label}:")
        lines.append(f"> {body}")
        lines.append("")

    lines += [
        "## Instructions",
        "",
        "1. Read each review comment above carefully.",
        "2. Make the requested changes in the workspace.",
        "3. Commit your changes with a message like `fix: address review comments`.",
        "4. Do NOT push — the system will handle that.",
        "5. When done, output a brief summary of what you changed.",
    ]
    return "\n".join(lines)


def moonlight_fix_pr(cfg: "Config", state: dict, repo: str, pr: dict) -> None:
    """If a hunter-created PR has unaddressed review comments, fix them."""
    if not cfg.moonlight_enabled:
        return

    branch = pr.get("headRefName", "")
    pr_number = pr["number"]
    key = f"{repo}#{pr_number}"

    # Only act on hunter-created branches
    if not branch.startswith(cfg.branch_prefix):
        return

    entry = state.get(key, {})
    turns_done = entry.get("moonlight_turns", 0)

    if turns_done >= cfg.max_moonlight_turns:
        return

    # Fetch review comments
    comments = _fetch_pr_review_comments(repo, pr_number)
    if not comments:
        return

    # Only act on comments newer than our last fix
    last_processed_id = entry.get("last_moonlight_review_id") or 0
    actionable = [
        c for c in comments
        if (c.get("id") or 0) > last_processed_id
        and (c.get("body") or c.get("state") == "CHANGES_REQUESTED")
    ]
    if not actionable:
        return

    logger.info("Moonlight: %d new review comment(s) on %s — starting fix (turn %d/%d)",
                len(actionable), key, turns_done + 1, cfg.max_moonlight_turns)

    # Find existing worktree or create fresh one.
    # Include the repo slug in the glob so we don't accidentally pick up a worktree
    # from a different repo with a similarly-named branch.
    worktree: Path | None = None
    worktree_base = Path(cfg.worktree_base)
    repo_slug = repo.replace("/", "-")
    branch_slug = branch.replace("/", "-").replace("_", "-")
    candidates = list(worktree_base.glob(f"{repo_slug}*{branch_slug[:40]}*"))
    if candidates:
        worktree = candidates[0]
        logger.debug("Moonlight: using existing worktree %s", worktree)
    else:
        try:
            base_branch = gh_repo_default_branch(repo)
            worktree = setup_new_branch_worktree(cfg, repo, branch, base_branch)
            logger.debug("Moonlight: created fresh worktree %s", worktree)
        except Exception as e:
            logger.error("Moonlight: could not create worktree for %s: %s", key, e)
            return

    # Mark state before running skill so we don't re-process the same comments
    # even if the process dies between push and state write.
    latest_id = max((c.get("id") or 0 for c in actionable), default=0)
    update_pr_state(state, key,
                    moonlight_turns=turns_done + 1,
                    last_moonlight_review_id=latest_id)

    prompt = _build_moonlight_prompt(repo, pr_number, branch, worktree, actionable)

    try:
        _run_skill_prompt(cfg, prompt, worktree)
    except Exception as e:
        logger.error("Moonlight: skill failed for %s: %s", key, e)
        # State already updated — we consumed this turn so we don't loop on the same error
        return

    # Push changes if any commits were made
    try:
        result = subprocess.run(
            ["git", "log", f"origin/{branch}..HEAD", "--oneline"],
            cwd=str(worktree), capture_output=True, text=True,
        )
        if result.stdout.strip():
            subprocess.run(
                ["git", "push", "origin", branch],
                cwd=str(worktree), check=True, capture_output=True,
            )
            logger.info("Moonlight: pushed fixes for %s", key)

            summary_comment = (
                f"Addressed review comments (moonlight fix, turn {turns_done + 1}/{cfg.max_moonlight_turns})."
            )
            try:
                gh_pr_comment(repo, pr_number, summary_comment)
            except Exception as e:
                logger.warning("Moonlight: could not post summary comment: %s", e)
        else:
            logger.info("Moonlight: skill ran but produced no commits for %s", key)
    except Exception as e:
        logger.error("Moonlight: push failed for %s: %s", key, e)
        return

    if turns_done + 1 >= cfg.max_moonlight_turns:
        exhausted_msg = (
            f"I've applied fixes {turns_done + 1} time(s) in response to review comments. "
            f"Please take another look and let me know if anything else needs changing."
        )
        try:
            gh_pr_comment(repo, pr_number, exhausted_msg)
        except Exception as e:
            logger.warning("Moonlight: could not post exhaustion comment: %s", e)

    log_decision("moonlight_fix", repo=repo, pr=pr_number, turn=turns_done + 1,
                 comments=len(actionable))


# ---------------------------------------------------------------------------
# PR processing
# ---------------------------------------------------------------------------

def process_pr(cfg: Config, state: dict, repo: str, pr: dict) -> None:
    pr_number = pr["number"]
    head_sha = pr["headRefOid"]
    head_ref = pr["headRefName"]
    title = pr["title"]
    key = f"{repo}#{pr_number}"

    logger.info("Processing %s: %s", key, title)
    log_decision("pr_review_started", repo=repo, pr=pr_number, sha=head_sha, title=title)
    update_pr_state(state, key, head_sha=head_sha, status="reviewing",
                    first_seen=_now_iso())

    # Pre-flight diff-size check — before any worktree setup
    if cfg.max_pr_diff_lines > 0:
        try:
            size_result = gh_run(
                ["pr", "view", str(pr_number), "--repo", repo, "--json", "additions,deletions"],
                check=False,
            )
            if size_result.returncode == 0:
                counts = json.loads(size_result.stdout)
                total = counts.get("additions", 0) + counts.get("deletions", 0)
                if total > cfg.max_pr_diff_lines:
                    msg = (
                        f"Skipping review — diff is too large ({total:,} lines changed, "
                        f"limit is {cfg.max_pr_diff_lines:,}).\n\n"
                        f"Review this PR manually. To raise the limit: "
                        f"`predd config set max_pr_diff_lines {total + 500}`"
                    )
                    logger.info("Skipping %s — diff too large (%d lines)", key, total)
                    gh_pr_comment(repo, pr_number, msg)
                    update_pr_state(state, key, status="rejected", head_sha=head_sha)
                    log_decision("pr_skip", repo=repo, pr=pr_number,
                                 reason="diff_too_large", lines=total)
                    return
        except Exception as e:
            logger.warning("Pre-flight diff check failed for %s: %s — proceeding", key, e)

    try:
        notify_sound(cfg.sound_new_pr)
        notify_toast("New PR", f"{key} — {title}")

        worktree = setup_worktree(cfg, repo, pr_number, head_sha, head_ref)
        logger.info("Worktree at %s", worktree)

        review_text = run_review(cfg, repo, pr_number, worktree)

        # Check for empty review output
        if not review_text or not review_text.strip():
            logger.error("Review skill produced no output for %s", key)
            if cfg.comment_on_failures:
                try:
                    comment = _PREDD_NO_OUTPUT_COMMENT.format(repo=repo, pr_number=pr_number)
                    gh_pr_comment(repo, pr_number, comment)
                    label = cfg.predd_failure_label.format(github_user=cfg.github_user)
                    gh_pr_add_label(repo, pr_number, label)
                    log_decision("pr_failure_commented", repo=repo, pr=pr_number, reason="no_output")
                except Exception as comment_err:
                    logger.error("Failed to post failure comment for %s: %s", key, comment_err)
            update_pr_state(state, key, status="failed")
            return

        # Save output for reference
        summary_path = worktree / "review-summary.md"
        summary_path.write_text(review_text)

        # Extract verdict from review output
        verdict = "COMMENT"
        for v in ("APPROVE", "REQUEST_CHANGES"):
            if v in review_text.upper():
                verdict = v
                break

        if cfg.predd_auto_post:
            verdict_to_type = {
                "APPROVE": "approve",
                "REQUEST_CHANGES": "request-changes",
                "COMMENT": "comment",
            }
            review_type = verdict_to_type[verdict]
            try:
                gh_pr_review(repo, pr_number, review_type, summary_path)
                logger.info("Auto-posted %s review for %s", verdict, key)
            except Exception as post_err:
                logger.warning("Failed to post review for %s, falling back to comment: %s", key, post_err)
                try:
                    gh_pr_comment(repo, pr_number, review_text)
                except Exception:
                    pass

        update_pr_state(state, key,
                        status="submitted",
                        worktree=str(worktree),
                        draft_path=str(summary_path),
                        review_completed=_now_iso())

        log_decision("pr_review_posted", repo=repo, pr=pr_number, sha=head_sha, verdict=verdict)
        notify_sound(cfg.sound_review_ready)
        notify_toast("Review posted", f"{key} — {verdict}")
        logger.info("Review complete for %s (verdict: %s)", key, verdict)

    except Exception as e:
        logger.error("Failed processing %s: %s", key, e, exc_info=True)
        if cfg.comment_on_failures:
            try:
                comment = _PREDD_CRASH_COMMENT.format(repo=repo, pr_number=pr_number, error=str(e))
                gh_pr_comment(repo, pr_number, comment)
                label = cfg.predd_failure_label.format(github_user=cfg.github_user)
                gh_pr_add_label(repo, pr_number, label)
                log_decision("pr_failure_commented", repo=repo, pr=pr_number, reason="crash", error=str(e))
            except Exception as comment_err:
                logger.error("Failed to post failure comment for %s: %s", key, comment_err)
        log_decision("pr_review_failed", repo=repo, pr=pr_number, error=str(e))
        update_pr_state(state, key, status="failed")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log_decision(event: str, **fields) -> None:
    """Append a structured decision record to decisions.jsonl."""
    record = {"ts": _now_iso(), "event": event, **fields}
    try:
        with open(DECISION_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # Never let decision logging crash the daemon


# ---------------------------------------------------------------------------
# PID management
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def acquire_pid_file() -> None:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if _pid_alive(pid):
                click.echo(f"predd already running (PID {pid}). Exiting.", err=True)
                sys.exit(1)
        except ValueError:
            pass
    PID_FILE.write_text(str(os.getpid()))


def release_pid_file() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# PR key parsing
# ---------------------------------------------------------------------------

def parse_pr_arg(pr_arg: str) -> tuple[str | None, int]:
    """Parse 'owner/repo#123' or '123'. Returns (repo_or_None, pr_number)."""
    m = re.match(r"^([^#]+)#(\d+)$", pr_arg)
    if m:
        return m.group(1), int(m.group(2))
    try:
        return None, int(pr_arg)
    except ValueError:
        raise click.BadParameter(f"Cannot parse PR '{pr_arg}'. Use 'owner/repo#123' or '123'.")


def resolve_pr_key(state: dict, pr_arg: str, cfg: Config | None = None) -> tuple[str, int, dict]:
    """Return (repo, pr_number, entry) from state."""
    repo_hint, pr_number = parse_pr_arg(pr_arg)

    # Search state for matching entry
    matches = []
    for key, entry in state.items():
        m = re.match(r"^(.+)#(\d+)$", key)
        if not m:
            continue
        repo, num = m.group(1), int(m.group(2))
        if num != pr_number:
            continue
        if repo_hint and repo != repo_hint:
            continue
        matches.append((repo, num, entry))

    if not matches:
        raise click.ClickException(f"PR {pr_arg} not found in state.")
    if len(matches) > 1:
        keys = [f"{r}#{n}" for r, n, _ in matches]
        raise click.ClickException(
            f"Ambiguous PR number {pr_number}. Specify repo: {', '.join(keys)}"
        )
    return matches[0]

# ---------------------------------------------------------------------------
# Status Server
# ---------------------------------------------------------------------------

from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse

_status_server_thread = None
_status_server_should_stop = False

class StatusHandler(BaseHTTPRequestHandler):
    """HTTP request handler for status page and API."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                html = generate_status_html()
                self.wfile.write(html.encode("utf-8"))
            elif path == "/api/status":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                data = get_status_json()
                self.wfile.write(json.dumps(data, indent=2).encode("utf-8"))
            else:
                self.send_error(404)
        except Exception as e:
            logger.error("Error handling status request: %s", e)
            self.send_error(500, str(e))

    def log_message(self, format, *args):
        """Suppress logging of HTTP requests."""
        pass


def load_recent_decisions(filepath: Path, limit: int = 20) -> list[dict]:
    """Load recent decision log entries."""
    if not filepath.exists():
        return []
    try:
        entries = []
        with open(filepath, "r") as f:
            for line in f:
                if line.strip():
                    entries.append(json.loads(line))
        return entries[-limit:] if limit else entries
    except Exception as e:
        logger.warning("Failed to load decisions from %s: %s", filepath, e)
        return []


def get_status_json() -> dict:
    """Generate JSON status data for API endpoint."""
    predd_state = load_state()
    hunter_state = {}
    try:
        if HUNTER_STATE_FILE.exists():
            hunter_state = json.loads(HUNTER_STATE_FILE.read_text())
    except Exception as e:
        logger.warning("Failed to load hunter state: %s", e)

    hunter_decisions = load_recent_decisions(HUNTER_DECISION_LOG, 20)
    predd_decisions = load_recent_decisions(DECISION_LOG, 20)

    # Compute summaries and group by status
    predd_by_status = {}
    for key, entry in predd_state.items():
        status = entry.get("status", "unknown")
        if status not in predd_by_status:
            predd_by_status[status] = []
        # Extract repo and PR number from key (format: owner/repo#N)
        m = re.match(r"^(.+)#(\d+)$", key)
        if m:
            repo, pr_num = m.group(1), m.group(2)
            predd_by_status[status].append({
                "pr": int(pr_num),
                "repo": repo,
                "status": status,
                "head_sha": entry.get("head_sha", "")[:8],
            })

    predd_summary = {
        "total": len(predd_state),
        "reviewing": len(predd_by_status.get("reviewing", [])),
        "submitted": len(predd_by_status.get("submitted", [])),
        "failed": len(predd_by_status.get("failed", [])),
    }

    # Group hunter issues by status
    hunter_by_status = {}
    for key, entry in hunter_state.items():
        status = entry.get("status", "unknown")
        if status not in hunter_by_status:
            hunter_by_status[status] = []
        # Extract repo and issue number from key (format: owner/repo!N)
        m = re.match(r"^(.+)!(\d+)$", key)
        if m:
            repo, issue_num = m.group(1), m.group(2)
            hunter_by_status[status].append({
                "issue": int(issue_num),
                "repo": repo,
                "status": status,
                "title": entry.get("title", ""),
            })

    hunter_summary = {
        "total": len(hunter_state),
        "in_progress": len(hunter_by_status.get("in_progress", [])),
        "proposal_open": len(hunter_by_status.get("proposal_open", [])),
        "implementing": len(hunter_by_status.get("implementing", [])),
        "self_reviewing": len(hunter_by_status.get("self_reviewing", [])),
        "ready_for_review": len(hunter_by_status.get("ready_for_review", [])),
        "submitted": len(hunter_by_status.get("submitted", [])),
        "failed": len(hunter_by_status.get("failed", [])),
    }

    return {
        "timestamp": _now_iso(),
        "predd": {
            "summary": predd_summary,
            "by_status": predd_by_status,
            "recent_decisions": predd_decisions[-10:],
        },
        "hunter": {
            "summary": hunter_summary,
            "by_status": hunter_by_status,
            "recent_decisions": hunter_decisions[-10:],
        },
    }


def format_decision(decision: dict) -> str:
    """Format a decision log entry as HTML activity item."""
    ts_str = decision.get("ts", "")
    event = decision.get("event", "unknown")

    # Parse timestamp to human-readable format (HH:MM)
    try:
        from datetime import datetime as dt
        dt_obj = dt.fromisoformat(ts_str.replace("Z", "+00:00"))
        time_str = dt_obj.strftime("%H:%M")
    except:
        time_str = ts_str[:16] if ts_str else "N/A"

    # Determine color and build description
    color = "gray"
    repo = decision.get("repo", "")

    # PR-related events
    if pr := decision.get("pr"):
        color = "blue" if "skip" in event else "green" if "posted" in event else "red" if "failed" in event else "yellow"
        pr_link = f'<a href="https://github.com/{repo}/pull/{pr}" target="_blank">#{pr}</a>' if repo else f"#{pr}"
        reason = decision.get("reason", "")
        verdict = decision.get("verdict", "")

        if event == "pr_skip":
            desc = f"Skipped PR {pr_link} ({reason})"
        elif event == "pr_review_started":
            desc = f"Started reviewing PR {pr_link}"
        elif event == "pr_review_posted":
            desc = f"Posted review on PR {pr_link} ({verdict})"
        elif event == "pr_review_failed":
            error = decision.get("error", "unknown error")[:50]
            desc = f"Review failed for PR {pr_link} ({error})"
        else:
            desc = f"PR {pr_link}: {event}"

    # Issue-related events
    elif issue := decision.get("issue"):
        color = "blue" if "skip" in event else "green" if event in ["proposal_merged", "issue_closed"] else "red" if "failed" in event else "yellow"
        issue_link = f'<a href="https://github.com/{repo}/issues/{issue}" target="_blank">#{issue}</a>' if repo else f"#{issue}"
        reason = decision.get("reason", "")
        status = decision.get("status", "")

        if event == "issue_skip":
            desc = f"Skipped issue {issue_link} ({reason})"
        elif event == "issue_pickup":
            desc = f"Picked up issue {issue_link}"
        elif event == "proposal_created":
            desc = f"Created proposal for issue {issue_link}"
        elif event == "proposal_merged":
            desc = f"Proposal merged for issue {issue_link}"
        elif event == "impl_created":
            desc = f"Created implementation for issue {issue_link}"
        elif event == "issue_closed":
            desc = f"Closed issue {issue_link}"
        elif event == "rollback":
            desc = f"Rolled back issue {issue_link}"
        else:
            desc = f"Issue {issue_link}: {event}"

    else:
        desc = event

    color_class = f"activity-{color}"
    return f'<div class="activity-item"><span class="activity-time">{time_str}</span> <span class="activity-event {color_class}">{desc}</span></div>'


def generate_status_html() -> str:
    """Generate the HTML status page."""
    status_data = get_status_json()
    status_json = json.dumps(status_data)

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Hunter & Predd Status</title>
    <style>
        :root {{
            --primary: #3b82f6;
            --green: #10b981;
            --yellow: #f59e0b;
            --red: #ef4444;
            --bg: #f9fafb;
            --fg: #1f2937;
            --fg-soft: #6b7280;
            --border: #e5e7eb;
            --card-bg: #ffffff;
            --card-shadow: 0 1px 3px rgba(0,0,0,0.1), 0 1px 2px rgba(0,0,0,0.06);
            --card-shadow-hover: 0 10px 15px rgba(0,0,0,0.1), 0 4px 6px rgba(0,0,0,0.05);
        }}
        @media (prefers-color-scheme: dark) {{
            :root {{
                --bg: #111827;
                --fg: #f3f4f6;
                --fg-soft: #d1d5db;
                --border: #374151;
                --card-bg: #1f2937;
            }}
        }}
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", sans-serif;
            background: var(--bg);
            color: var(--fg);
            margin: 0;
            padding: 16px;
            line-height: 1.6;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        .header {{
            margin-bottom: 32px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border);
        }}
        h1 {{ margin: 0 0 4px; font-size: 28px; font-weight: 700; }}
        .header-meta {{ font-size: 13px; color: var(--fg-soft); }}
        h2 {{ font-size: 18px; font-weight: 600; margin: 28px 0 16px; }}
        .cards {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
            gap: 12px;
            margin-bottom: 24px;
        }}
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px;
            box-shadow: var(--card-shadow);
            transition: all 0.2s ease;
        }}
        .card.clickable {{
            cursor: pointer;
        }}
        .card.clickable:hover {{
            box-shadow: var(--card-shadow-hover);
            transform: translateY(-2px);
            border-color: var(--primary);
        }}
        .card-label {{
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            color: var(--fg-soft);
            margin-bottom: 8px;
        }}
        .card-count {{
            font-size: 32px;
            font-weight: 700;
            color: var(--fg);
        }}
        .modal {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.5);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }}
        .modal.open {{ display: flex; }}
        .modal-content {{
            background: var(--card-bg);
            border-radius: 12px;
            max-width: 600px;
            width: 90%;
            max-height: 80vh;
            overflow-y: auto;
            box-shadow: 0 20px 25px rgba(0,0,0,0.15);
        }}
        .modal-header {{
            padding: 20px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .modal-title {{ font-size: 18px; font-weight: 600; margin: 0; }}
        .modal-close {{
            background: none;
            border: none;
            font-size: 24px;
            cursor: pointer;
            color: var(--fg-soft);
            padding: 0;
            width: 32px;
            height: 32px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .modal-body {{
            padding: 20px;
        }}
        .modal-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }}
        .modal-table th {{
            text-align: left;
            padding: 12px;
            font-weight: 600;
            border-bottom: 2px solid var(--border);
            background: var(--bg);
        }}
        .modal-table td {{
            padding: 12px;
            border-bottom: 1px solid var(--border);
        }}
        .modal-table tr:last-child td {{ border-bottom: none; }}
        .modal-table a {{
            color: var(--primary);
            text-decoration: none;
            font-weight: 500;
        }}
        .modal-table a:hover {{ text-decoration: underline; }}
        .activity {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px;
            box-shadow: var(--card-shadow);
        }}
        .activity-item {{
            padding: 12px 0;
            border-bottom: 1px solid var(--border);
            font-size: 14px;
        }}
        .activity-item:last-child {{ border-bottom: none; }}
        .activity-time {{
            font-weight: 600;
            color: var(--fg-soft);
            min-width: 60px;
            display: inline-block;
        }}
        .activity-text {{ color: var(--fg); }}
        .activity-text a {{
            color: var(--primary);
            text-decoration: none;
        }}
        .activity-text a:hover {{ text-decoration: underline; }}
        .activity-event {{
            display: inline-block;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 500;
        }}
        .activity-green {{ background: rgba(16, 185, 129, 0.1); color: var(--green); }}
        .activity-red {{ background: rgba(239, 68, 68, 0.1); color: var(--red); }}
        .activity-blue {{ background: rgba(59, 130, 246, 0.1); color: var(--primary); }}
        .activity-yellow {{ background: rgba(245, 158, 11, 0.1); color: var(--yellow); }}
        .activity-gray {{ background: rgba(107, 114, 128, 0.1); color: var(--fg-soft); }}
        .status-failed {{
            color: var(--red);
            font-weight: 600;
        }}
        .status-submitted {{
            color: var(--green);
            font-weight: 600;
        }}
        .status-implementing {{
            color: var(--yellow);
            font-weight: 600;
        }}
        .status-proposal_open {{
            color: var(--yellow);
            font-weight: 600;
        }}
        .status-in_progress {{
            color: var(--primary);
            font-weight: 600;
        }}
        @media (max-width: 640px) {{
            .cards {{ grid-template-columns: repeat(2, 1fr); }}
            .modal-content {{ width: 95%; }}
        }}
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>Hunter & Predd Status</h1>
        <div class="header-meta">Last updated <span id="timestamp-display"></span></div>
    </div>

    <section>
        <h2>Predd (PR Reviews)</h2>
        <div class="cards" id="predd-cards"></div>
    </section>

    <section>
        <h2>Hunter (Issues)</h2>
        <div class="cards" id="hunter-cards"></div>
    </section>

    <section>
        <h2>Recent Activity</h2>
        <div class="activity" id="activity"></div>
    </section>
</div>

<div class="modal" id="modal">
    <div class="modal-content">
        <div class="modal-header">
            <h3 class="modal-title" id="modal-title"></h3>
            <button class="modal-close" onclick="closeModal()">✕</button>
        </div>
        <div class="modal-body" id="modal-body"></div>
    </div>
</div>

<script>
const statusData = {status_json};
const modalData = {{}};

function renderPage() {{
    const timestamp = new Date(statusData.timestamp);
    document.getElementById('timestamp-display').textContent = timestamp.toLocaleTimeString();

    renderCards('predd');
    renderCards('hunter');
    renderActivity();
}}

function renderCards(type) {{
    const data = statusData[type];
    const container = document.getElementById(type + '-cards');
    const statusOrder = type === 'predd'
        ? ['reviewing', 'submitted', 'failed']
        : ['in_progress', 'proposal_open', 'implementing', 'self_reviewing', 'ready_for_review', 'submitted', 'failed'];

    let html = `<div class="card"><div class="card-label">Total</div><div class="card-count">${{data.summary.total}}</div></div>`;

    for (const status of statusOrder) {{
        const count = data.summary[status] || 0;
        const items = data.by_status[status] || [];
        const label = status.replace(/_/g, ' ').replace(/\\b\\w/g, c => c.toUpperCase());

        if (count > 0) {{
            const key = type + '_' + status;
            modalData[key] = {{ label, items, type }};
            html += `<div class="card clickable" onclick="showModal('${{key}}')">
                <div class="card-label">${{label}}</div>
                <div class="card-count">${{count}}</div>
            </div>`;
        }} else {{
            html += `<div class="card"><div class="card-label">${{label}}</div><div class="card-count">0</div></div>`;
        }}
    }}

    container.innerHTML = html;
}}

function showModal(key) {{
    const data = modalData[key];
    if (!data) return;

    const modal = document.getElementById('modal');
    document.getElementById('modal-title').textContent = data.label;

    let html = '<table class="modal-table"><thead><tr>';
    if (data.type === 'predd') {{
        html += '<th>PR</th><th>Repo</th>';
    }} else {{
        html += '<th>Issue</th><th>Title</th>';
    }}
    html += '</tr></thead><tbody>';

    for (const item of data.items) {{
        if (data.type === 'predd') {{
            html += `<tr><td><a href="https://github.com/${{item.repo}}/pull/${{item.pr}}" target="_blank">#${{item.pr}}</a></td><td>${{item.repo}}</td></tr>`;
        }} else {{
            html += `<tr><td><a href="https://github.com/${{item.repo}}/issues/${{item.issue}}" target="_blank">#${{item.issue}}</a></td><td>${{item.title.substring(0, 50)}}</td></tr>`;
        }}
    }}

    html += '</tbody></table>';
    document.getElementById('modal-body').innerHTML = html;
    modal.classList.add('open');
}}

function closeModal() {{
    document.getElementById('modal').classList.remove('open');
}}

function renderActivity() {{
    const decisions = [...statusData.predd.recent_decisions, ...statusData.hunter.recent_decisions]
        .sort((a, b) => new Date(b.ts) - new Date(a.ts))
        .slice(0, 20);

    let html = '';
    if (decisions.length === 0) {{
        html = '<div style="color: var(--fg-soft); font-style: italic;">No recent activity</div>';
    }} else {{
        for (const d of decisions) {{
            const time = new Date(d.ts).toLocaleTimeString([], {{hour: '2-digit', minute: '2-digit'}});
            let text = '';
            let color = 'gray';

            // Determine color based on event type
            if (d.event.includes('failed') || d.event.includes('Failed')) {{
                color = 'red';
            }} else if (d.event.includes('posted') || d.event.includes('merged') || d.event.includes('closed')) {{
                color = 'green';
            }} else if (d.event.includes('started') || d.event.includes('pickup') || d.event.includes('created')) {{
                color = 'blue';
            }} else if (d.event.includes('skip')) {{
                color = 'yellow';
            }}

            if (d.pr) {{
                const reason = d.reason ? ` - ${{d.reason}}` : '';
                text = `Skipped PR <a href="https://github.com/${{d.repo}}/pull/${{d.pr}}" target="_blank">#${{d.pr}}</a>${{reason}}`;
            }} else if (d.issue) {{
                text = `Issue <a href="https://github.com/${{d.repo}}/issues/${{d.issue}}" target="_blank">#${{d.issue}}</a> ${{d.event}}`;
            }} else {{
                text = d.event;
            }}

            html += `<div class="activity-item"><span class="activity-time">${{time}}</span> <span class="activity-event activity-${{color}}">${{text}}</span></div>`;
        }}
    }}

    document.getElementById('activity').innerHTML = html;
}}

document.getElementById('modal').addEventListener('click', (e) => {{
    if (e.target.id === 'modal') closeModal();
}});

renderPage();
setInterval(refreshData, 10000);

async function refreshData() {{
    try {{
        const res = await fetch('/api/status');
        const data = await res.json();
        Object.assign(statusData, data);
        renderPage();
    }} catch (e) {{
        console.error('Failed to refresh:', e);
    }}
}}
</script>
</body>
</html>"""


def start_status_server(port: int) -> threading.Thread | None:
    """Start the status server in a daemon thread."""
    global _status_server_thread
    try:
        server = HTTPServer(("localhost", port), StatusHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="status-server")
        thread.start()
        logger.info("Status server started on http://localhost:%d", port)
        _status_server_thread = (server, thread)
        return thread
    except Exception as e:
        logger.error("Failed to start status server: %s", e)
        return None


def stop_status_server():
    """Stop the status server if running."""
    global _status_server_thread
    if _status_server_thread:
        server, thread = _status_server_thread
        try:
            server.shutdown()
            thread.join(timeout=2)
        except Exception as e:
            logger.warning("Error stopping status server: %s", e)
        _status_server_thread = None


# ---------------------------------------------------------------------------
# Bedrock Agent Backend
# ---------------------------------------------------------------------------

BEDROCK_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a text file in the worktree",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to worktree)"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_files",
        "description": "List files in worktree directory",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: .)"}
            }
        }
    },
    {
        "name": "bash",
        "description": "Run bash command in worktree",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to run"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)"}
            },
            "required": ["command"]
        }
    }
]


def _handle_bedrock_tool(name: str, input_data: dict, worktree: Path) -> str:
    """Execute a bedrock tool."""
    try:
        if name == "read_file":
            path = worktree / input_data.get("path", "")
            content = path.read_text()
            limit = 30_000
            if len(content) > limit:
                content = content[:limit] + f"\n... [truncated — {len(content) - limit} chars omitted]"
            return content

        if name == "list_files":
            target = worktree / input_data.get("path", ".")
            files = sorted(
                str(p.relative_to(target))
                for p in target.rglob("*")
                if p.is_file() and not any(part.startswith(".") for part in p.parts)
            )
            return "\n".join(files) or "(empty)"

        if name == "bash":
            result = subprocess.run(
                input_data["command"],
                shell=True,
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=input_data.get("timeout", 120)
            )
            out = result.stdout or ""
            if result.stderr:
                out += f"\n--- stderr ---\n{result.stderr}"
            output = f"exit={result.returncode}\n{out}"
            # Truncate large outputs to avoid exceeding model context window
            limit = 30_000
            if len(output) > limit:
                output = output[:limit] + f"\n... [truncated — {len(output) - limit} chars omitted]"
            return output

        return f"Unknown tool: {name}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


def _run_bedrock_skill(cfg: Config, prompt: str, skill_path: Path, worktree: Path) -> str:
    """Run skill via AWS Bedrock Claude with agentic tool use."""
    try:
        from anthropic import AnthropicBedrock
    except ImportError:
        raise RuntimeError("anthropic[bedrock] not installed. Run: pip install -U 'anthropic[bedrock]'")

    # Load SKILL.md
    skill_text = skill_path.read_text()

    # Create Bedrock client — set AWS_PROFILE in env for boto3 credential chain
    if cfg.aws_profile and cfg.aws_profile != "default":
        os.environ["AWS_PROFILE"] = cfg.aws_profile

    client = AnthropicBedrock(aws_region=cfg.aws_region)

    # Build system prompt with prompt caching on the SKILL.md block
    system = [
        {
            "type": "text",
            "text": (
                "You are an AI engineer assistant. "
                "Follow the SKILL.md to complete the task."
            ),
        },
        {
            "type": "text",
            "text": f"--- SKILL.md ---\n{skill_text}\n--- end SKILL.md ---",
            "cache_control": {"type": "ephemeral"},
        },
    ]

    # Agentic loop
    messages = [{"role": "user", "content": prompt}]
    max_turns = 50

    for turn in range(max_turns):
        resp = client.messages.create(
            model=cfg.bedrock_model,
            max_tokens=4096,
            system=system,
            tools=BEDROCK_TOOLS,
            messages=messages
        )

        # Collect text output
        output_text = ""
        has_tool_use = False

        for block in resp.content:
            if hasattr(block, "text") and block.text:
                output_text += block.text
            if hasattr(block, "type") and block.type == "tool_use":
                has_tool_use = True

        if output_text:
            logger.debug("Bedrock output: %s", output_text[:200])

        # If stop reason isn't tool_use, we're done
        if resp.stop_reason != "tool_use":
            return output_text or "Task completed"

        # Process tool calls
        tool_results = []
        for block in resp.content:
            if hasattr(block, "type") and block.type == "tool_use":
                tool_name = block.name
                tool_input = block.input
                logger.debug("Bedrock tool call: %s(%s)", tool_name, tool_input)

                result = _handle_bedrock_tool(tool_name, tool_input, worktree)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        # Continue conversation with tool results
        # Serialize only API-accepted fields — model_dump() includes internal SDK fields (e.g. "caller") that Bedrock rejects
        def _serialize_block(b):
            if hasattr(b, "type") and b.type == "tool_use":
                return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
            elif hasattr(b, "type") and b.type == "text":
                return {"type": "text", "text": b.text}
            elif hasattr(b, "model_dump"):
                return b.model_dump()
            else:
                return {"type": b.type}
        messages.append({"role": "assistant", "content": [_serialize_block(b) for b in resp.content]})
        messages.append({"role": "user", "content": tool_results})

    logger.warning("Bedrock hit max turns (%d)", max_turns)
    return output_text or "Max turns reached"

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """predd: daemon that drafts PR reviews for human approval."""
    pass


@cli.command(name="status-server")
@click.option("--port", type=int, default=None, help="Port to run status server on (default from config).")
def status_server_cmd(port: int):
    """Start the status server (without polling daemon)."""
    setup_logging()
    cfg = load_config()

    if port is None:
        port = cfg.status_port

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    _stop.clear()

    logger.info("Status server starting on port %d", port)

    if start_status_server(port):
        click.echo(f"Status server running at http://localhost:{port}")
        try:
            while not _stop.is_set():
                _stop.wait(1)
        finally:
            stop_status_server()
    else:
        raise click.ClickException("Failed to start status server")


# ---------------------------------------------------------------------------
# Obsidian Observation System
# ---------------------------------------------------------------------------

def obsidian_observe() -> None:
    """Observe and record activity patterns for self-improvement analysis."""
    obsidian_dir = CONFIG_DIR / "obsidian"
    obsidian_dir.mkdir(exist_ok=True)

    # Load all state
    predd_state = load_state()
    hunter_state = {}
    try:
        if HUNTER_STATE_FILE.exists():
            hunter_state = json.loads(HUNTER_STATE_FILE.read_text())
    except:
        pass

    # Load recent decisions
    predd_decisions = load_recent_decisions(DECISION_LOG, 100)
    hunter_decisions = load_recent_decisions(HUNTER_DECISION_LOG, 100)

    # Analyze patterns
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    note_file = obsidian_dir / f"{today}-observation.md"

    # Count events by type
    predd_events = {}
    for d in predd_decisions:
        event = d.get("event", "unknown")
        predd_events[event] = predd_events.get(event, 0) + 1

    hunter_events = {}
    for d in hunter_decisions:
        event = d.get("event", "unknown")
        hunter_events[event] = hunter_events.get(event, 0) + 1

    # Identify issues with high rollback rate
    rollback_count = hunter_events.get("rollback", 0)
    issue_closed_count = hunter_events.get("issue_closed", 0)
    claim_failed_count = hunter_events.get("claim_failed", 0)

    # Find failures in current state
    failed_issues = [k for k, v in hunter_state.items() if v.get("status") == "failed"]
    failed_prs = [k for k, v in predd_state.items() if v.get("status") == "failed"]

    # Build observation note
    note = f"""# Observation: {today}

## Summary
- **Predd Reviews**: {predd_events.get('pr_review_posted', 0)} posted, {predd_events.get('pr_skip', 0)} skipped
- **Hunter Issues**: {len(hunter_state)} tracked, {issue_closed_count} closed
- **Rollbacks**: {rollback_count} (failure rate: {int(rollback_count/(len(hunter_state)+1)*100)}%)

## Predd (PR Reviews)
Total PRs tracked: {len(predd_state)}
- Submitted: {sum(1 for v in predd_state.values() if v.get('status') == 'submitted')}
- Reviewing: {sum(1 for v in predd_state.values() if v.get('status') == 'reviewing')}
- Failed: {len(failed_prs)}

Skip reasons (top):
"""
    skip_reasons = {}
    for d in predd_decisions:
        if d.get("event") == "pr_skip":
            reason = d.get("reason", "unknown")
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1])[:5]:
        note += f"- {reason}: {count}\n"

    note += f"""
## Hunter (Issues)
Total issues tracked: {len(hunter_state)}
- Closed: {issue_closed_count}
- Proposal merged: {hunter_events.get('proposal_merged', 0)}
- Failed: {len(failed_issues)}
- Claim failures: {claim_failed_count}
- Rollbacks: {rollback_count}

Issue status distribution:
"""
    status_counts = {}
    for v in hunter_state.values():
        status = v.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
        note += f"- {status}: {count}\n"

    # Identify patterns
    note += f"""
## Patterns & Observations

### Success Rate
- Issues closed / total: {issue_closed_count} / {len(hunter_state)} = {int(issue_closed_count/(len(hunter_state)+1)*100)}%
- High rollback rate ({rollback_count}) suggests implementation issues

### Key Issues
"""
    if claim_failed_count > 5:
        note += f"- **Claim failures elevated** ({claim_failed_count}): Issues can't be claimed\n"
    if rollback_count > len(hunter_state) * 0.2:
        note += f"- **High rollback rate**: {int(rollback_count/(len(hunter_state)+1)*100)}% of issues fail\n"
    if skip_reasons.get("already_reviewed_same_sha", 0) > predd_events.get("pr_review_posted", 1):
        note += "- **PR cache hit**: Most PRs already reviewed, low new work\n"

    note += f"""
## Questions for Analysis
1. Why do {rollback_count} issues rollback? What's the common failure?
2. What makes the {issue_closed_count} successful closures different?
3. Can claim failures be prevented by better issue detection?
4. Are PR reviews missing important patterns?

---
Generated at {_now_iso()}
"""

    note_file.write_text(note)
    click.echo(f"✓ Observation written to {note_file}")
    click.echo(f"\nKey metrics:")
    click.echo(f"  Predd: {len(predd_state)} PRs, {predd_events.get('pr_review_posted', 0)} reviews")
    click.echo(f"  Hunter: {len(hunter_state)} issues, {issue_closed_count} closed, {rollback_count} rollbacks")


@cli.command(name="observe")
def observe_cmd():
    """Observe current activity and write to obsidian vault."""
    obsidian_observe()


@cli.command(name="analyze")
@click.option("--model", default=None, help="Model to use for analysis (default from config)")
@click.option("--days", default=7, help="Days of observations to analyze")
def analyze_cmd(model: str, days: int):
    """Analyze observations and generate improvement specs."""
    cfg = load_config()
    if model is None:
        model = cfg.analyze_model

    obsidian_analyze(cfg, model, days)


def obsidian_analyze(cfg: Config, model: str, days: int = 7) -> None:
    """Analyze observation patterns and generate improvement specs."""
    obsidian_dir = CONFIG_DIR / "obsidian"
    obsidian_dir.mkdir(exist_ok=True)
    spec_dir = Path(__file__).parent / "spec" / "changes"
    spec_dir.mkdir(parents=True, exist_ok=True)

    # Read observations from last N days
    observations = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    for obs_file in sorted(obsidian_dir.glob("*-observation.md")):
        try:
            file_date = datetime.strptime(obs_file.stem.split("-observation")[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if file_date >= cutoff:
                observations.append(obs_file.read_text())
        except:
            pass

    if not observations:
        click.echo(f"✗ No observations found in last {days} days")
        return

    click.echo(f"✓ Found {len(observations)} observations")
    click.echo(f"✓ Using model: {model}")
    click.echo(f"\nAnalyzing patterns...")

    # Build analysis prompt
    obs_text = "\n\n---\n\n".join(observations)

    analysis_prompt = f"""You are analyzing activity logs from an automated software engineering system (predd for PR reviews, hunter for issue implementation).

Based on these observations from the last {days} days:

{obs_text}

## Your Task

Analyze the patterns and generate 1-3 concrete improvement specs that can be implemented.

## Output Format

Output a JSON object with this exact structure (no markdown fencing, raw JSON only):

{{
  "analysis": "Brief summary of what you found (2-3 sentences)",
  "specs": [
    {{
      "filename": "short-kebab-case-name.md",
      "title": "Human readable title",
      "content": "Full spec content in markdown. Include: ## Problem, ## Solution, ## Implementation, ## Testing sections."
    }}
  ]
}}

Rules:
- filename must be kebab-case, end in .md
- content must be actionable — specific enough for an AI agent to implement
- Focus on the highest-impact improvements based on the failure patterns
- Do not propose changes that are already working well
- Each spec should be a single, focused change"""

    # Call Claude/Sonnet to analyze
    click.echo("Calling Claude for analysis...")

    try:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        result = subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions", "--model", model],
            input=analysis_prompt,
            capture_output=True,
            text=True,
            env=env,
            timeout=120
        )

        if result.returncode != 0:
            click.echo(f"✗ Claude error: {result.stderr}")
            return

        analysis_text = result.stdout
    except subprocess.TimeoutExpired:
        click.echo("✗ Analysis timed out")
        return
    except Exception as e:
        click.echo(f"✗ Error: {e}")
        return

    # Parse JSON output and write specs to spec/changes/
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Strip markdown fencing if present
    json_text = analysis_text.strip()
    if json_text.startswith("```"):
        json_text = "\n".join(json_text.split("\n")[1:])
        if json_text.endswith("```"):
            json_text = json_text[:-3].strip()

    try:
        result_data = json.loads(json_text)
    except json.JSONDecodeError:
        # Fall back: save raw analysis to obsidian dir
        fallback = obsidian_dir / f"{today}-analysis.md"
        fallback.write_text(analysis_text)
        click.echo(f"✗ Could not parse structured output — raw analysis saved to {fallback}")
        return

    # Write analysis summary to obsidian dir for reference
    analysis_file = obsidian_dir / f"{today}-analysis.md"
    analysis_file.write_text(f"# Analysis: {today}\n\n{result_data.get('analysis', '')}\n\n---\nModel: {model} | Observations: {len(observations)} over {days} days\n")
    click.echo(f"\n✓ Analysis summary written to {analysis_file}")

    # Write each spec to spec/changes/
    specs = result_data.get("specs", [])
    if not specs:
        click.echo("✗ No improvement specs generated")
        return

    written = 0
    for spec in specs:
        filename = spec.get("filename", "").strip()
        title = spec.get("title", "Untitled")
        content = spec.get("content", "")

        if not filename or not content:
            continue

        spec_file = spec_dir / filename
        if spec_file.exists():
            click.echo(f"  ⊘ {filename} already exists — skipping")
            continue

        spec_file.write_text(f"# {title}\n\n{content}\n")
        click.echo(f"  ✓ spec/changes/{filename}")
        log_decision("spec_generated", spec=filename, title=title, model=model)
        written += 1

    click.echo(f"\n✓ {written} spec(s) written to spec/changes/")


@cli.command()
@click.option("--once", is_flag=True, help="Run a single poll iteration then exit.")
def start(once: bool):
    """Run the polling daemon."""
    setup_logging()
    cfg = load_config()
    cfg.worktree_base.mkdir(parents=True, exist_ok=True)

    if not once:
        acquire_pid_file()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    _stop.clear()
    _current_pr_key[:] = []

    logger.info("predd started (once=%s)", once)

    if not once and cfg.status_server_enabled:
        start_status_server(cfg.status_port)

    try:
        while not _stop.is_set():
            state = load_state()
            for repo in cfg.repos_for("predd"):
                if _stop.is_set():
                    break
                try:
                    prs = gh_list_open_prs(repo)
                except Exception as e:
                    logger.error("Failed to list PRs for %s: %s", repo, e)
                    continue

                for pr in prs:
                    if _stop.is_set():
                        break
                    pr_number = pr["number"]
                    head_sha = pr["headRefOid"]
                    key = f"{repo}#{pr_number}"

                    if pr["author"]["login"] == cfg.github_user:
                        log_decision("pr_skip", repo=repo, pr=pr_number, reason="own_pr")
                        continue
                    if pr["isDraft"]:
                        log_decision("pr_skip", repo=repo, pr=pr_number, reason="draft")
                        continue
                    if cfg.trigger == "requested":
                        requested = [r.get("login") for r in pr.get("reviewRequests", [])]
                        if cfg.github_user not in requested:
                            log_decision("pr_skip", repo=repo, pr=pr_number, reason="not_requested")
                            continue

                    entry = state.get(key, {})
                    entry_sha = entry.get("head_sha", "")
                    entry_status = entry.get("status", "")

                    if entry_sha == head_sha and entry_status in (
                        "rejected", "awaiting_approval", "reviewing"
                    ):
                        log_decision("pr_skip", repo=repo, pr=pr_number, reason=f"status_{entry_status}")
                        continue
                    # submitted with same SHA → skip; new SHA → re-review
                    if entry_sha == head_sha and entry_status == "submitted":
                        log_decision("pr_skip", repo=repo, pr=pr_number, reason="already_reviewed_same_sha")
                        continue

                    if gh_pr_already_reviewed(repo, pr_number, cfg.github_user):
                        logger.info("Skipping %s — already reviewed or closed", key)
                        log_decision("pr_skip", repo=repo, pr=pr_number, reason="already_reviewed_by_user")
                        update_pr_state(state, key, head_sha=head_sha, status="rejected")
                        continue

                    _current_pr_key[:] = [key]
                    process_pr(cfg, state, repo, pr)
                    _current_pr_key.clear()

            # --- Moonlight pass — fix hunter PRs with review comments ---
            if cfg.moonlight_enabled and not _stop.is_set():
                state = load_state()
                for repo in cfg.repos_for("predd"):
                    if _stop.is_set():
                        break
                    try:
                        prs = gh_list_open_prs(repo)
                    except Exception as e:
                        logger.warning("Moonlight: could not list PRs for %s: %s", repo, e)
                        continue
                    for pr in prs:
                        if _stop.is_set():
                            break
                        if not pr.get("headRefName", "").startswith(cfg.branch_prefix):
                            continue
                        try:
                            moonlight_fix_pr(cfg, state, repo, pr)
                            state = load_state()
                        except Exception as e:
                            logger.error("Moonlight error on %s#%s: %s",
                                         repo, pr.get("number"), e)

            # --- Sentinel post-CI pass ---
            if cfg.post_ci_review_enabled and not _stop.is_set():
                _sentinel = None
                try:
                    import importlib.util as _ilu
                    _s_spec = _ilu.spec_from_file_location(
                        "sentinel", Path(__file__).resolve().parent / "sentinel.py"
                    )
                    _sentinel = _ilu.module_from_spec(_s_spec)
                    _s_spec.loader.exec_module(_sentinel)
                except Exception as _e:
                    logger.warning("Could not load sentinel: %s", _e)

                if _sentinel:
                    state = load_state()
                    for _pr_key, _pr_entry in list(state.items()):
                        if _stop.is_set():
                            break
                        if _pr_entry.get("status") == "submitted" and not _pr_entry.get("post_ci_reviewed"):
                            _repo_from_key = _pr_key.rsplit("#", 1)[0]
                            _pr_num_from_key = int(_pr_key.rsplit("#", 1)[1])
                            try:
                                _sentinel.run_post_ci_review(cfg, state, _repo_from_key, _pr_num_from_key)
                                state = load_state()
                            except Exception as _e:
                                logger.error("Sentinel error on %s: %s", _pr_key, _e)

            if once or _stop.wait(cfg.poll_interval):
                break

        logger.info("Shutting down cleanly.")
    finally:
        stop_status_server()
        if not once:
            release_pid_file()


@cli.command(name="list")
def list_cmd():
    """Print pending reviews as JSON."""
    state = load_state()
    pending = {k: v for k, v in state.items() if v.get("status") == "awaiting_approval"}
    click.echo(json.dumps(pending, indent=2))


@cli.command()
@click.argument("pr")
def show(pr: str):
    """Print the draft review for a PR."""
    state = load_state()
    repo, pr_number, entry = resolve_pr_key(state, pr)
    draft_path = entry.get("draft_path")
    if not draft_path or not Path(draft_path).exists():
        raise click.ClickException("Draft not found. Has the review completed?")
    click.echo(Path(draft_path).read_text())


def _submit_review(pr_arg: str, review_type: str) -> None:
    state = load_state()
    repo, pr_number, entry = resolve_pr_key(state, pr_arg)
    key = f"{repo}#{pr_number}"

    if entry.get("status") != "awaiting_approval":
        raise click.ClickException(
            f"PR is in status '{entry.get('status')}', not awaiting_approval."
        )

    draft_path = Path(entry["draft_path"])
    if not draft_path.exists():
        raise click.ClickException(f"Draft file not found: {draft_path}")

    # Extract body: everything after the first ## heading (skip Verdict line area)
    draft_text = draft_path.read_text()
    # Use from ## Summary onward as the review body
    m = re.search(r"(## Summary.*)", draft_text, re.DOTALL)
    body_text = m.group(1) if m else draft_text

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body_text)
        body_file = Path(f.name)

    try:
        gh_pr_review(repo, pr_number, review_type, body_file)
    finally:
        body_file.unlink(missing_ok=True)

    state[key]["status"] = "submitted"
    save_state(state)

    worktree = entry.get("worktree")
    if worktree:
        try:
            remove_worktree(worktree)
        except Exception as e:
            logger.warning("Failed to remove worktree %s: %s", worktree, e)

    click.echo("Submitted.")


@cli.command()
@click.argument("pr")
def approve(pr: str):
    """Submit draft as approval."""
    _submit_review(pr, "approve")


@cli.command()
@click.argument("pr")
def comment(pr: str):
    """Submit draft as comment-only."""
    _submit_review(pr, "comment")


@cli.command(name="request-changes")
@click.argument("pr")
def request_changes(pr: str):
    """Submit draft as request-changes."""
    _submit_review(pr, "request-changes")


@cli.command()
@click.argument("pr")
def reject(pr: str):
    """Discard draft and mark PR as reviewed without submitting."""
    state = load_state()
    repo, pr_number, entry = resolve_pr_key(state, pr)
    key = f"{repo}#{pr_number}"

    state[key]["status"] = "rejected"
    save_state(state)

    worktree = entry.get("worktree")
    if worktree:
        try:
            remove_worktree(worktree)
        except Exception as e:
            logger.warning("Failed to remove worktree %s: %s", worktree, e)

    click.echo("Discarded.")


@cli.command(name="init")
@click.option("--force", is_flag=True, help="Overwrite existing config without prompting.")
@click.option("--ui", is_flag=True, help="Serve a web UI instead of terminal wizard.")
def init_cmd(force: bool, ui: bool):
    """Interactive config wizard."""
    if ui:
        click.echo("Web UI not yet implemented.")
        return
    run_config_wizard(force=force)


@cli.group(name="config", invoke_without_command=True)
@click.pass_context
def config_group(ctx):
    """Show or update configuration."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(config_show)


@config_group.command(name="show")
def config_show():
    """Print current config as a human-readable table."""
    if not CONFIG_FILE.exists():
        raise click.ClickException(f"Config file not found: {CONFIG_FILE}")
    cfg = load_config()
    d = cfg.to_dict()
    repo_list = d.pop("repo", [])

    col = 30
    for k, v in d.items():
        if isinstance(v, list):
            items = ", ".join(str(x) for x in v)
            click.echo(f"{k:<{col}} [{items}]")
        else:
            click.echo(f"{k:<{col}} {v}")

    if repo_list:
        click.echo("\nRepos:")
        for rc in repo_list:
            click.echo(
                f"  {rc['name']:<40} predd={str(rc.get('predd', True)).lower():<5} "
                f"hunter={str(rc.get('hunter', True)).lower():<5} "
                f"obsidian={str(rc.get('obsidian', False)).lower():<5}"
            )


_CONFIG_SET_KEYS = {
    "github_user": str,
    "worktree_base": str,
    "backend": str,
    "model": str,
    "trigger": str,
    "max_review_fix_loops": int,
    "auto_review_draft": lambda v: v.lower() in ("true", "1", "yes"),
    "max_resume_retries": int,
    "max_new_issues_per_cycle": int,
    "orphan_scan_interval": int,
    "auto_label_prs": lambda v: v.lower() in ("true", "1", "yes"),
    "collect_pr_feedback": lambda v: v.lower() in ("true", "1", "yes"),
    "branch_prefix": str,
    "jira_base_url": str,
    "jira_api_enabled": lambda v: v.lower() in ("true", "1", "yes"),
    "jira_sprint_filter": str,
}


@config_group.command(name="set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str):
    """Update a single config field."""
    if key not in _CONFIG_SET_KEYS:
        raise click.ClickException(
            f"Unknown config key: {key!r}. Supported keys: {', '.join(sorted(_CONFIG_SET_KEYS))}"
        )
    if not CONFIG_FILE.exists():
        raise click.ClickException(f"Config file not found: {CONFIG_FILE}")

    with open(CONFIG_FILE, "rb") as f:
        data = tomllib.load(f)

    converter = _CONFIG_SET_KEYS[key]
    try:
        data[key] = converter(value)
    except (ValueError, TypeError) as e:
        raise click.ClickException(f"Invalid value for {key}: {e}")

    # Rebuild via Config to validate, then re-serialize
    try:
        cfg = Config(data)
    except Exception as e:
        raise click.ClickException(f"Config validation failed: {e}")

    cfg_dict = cfg.to_dict()
    _write_config_atomic(cfg_dict)
    click.echo(f"Set {key} = {data[key]}")


if __name__ == "__main__":
    cli()
