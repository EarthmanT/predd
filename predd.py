#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "click",
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
from datetime import datetime, timezone
from pathlib import Path

import click

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_DIR = Path("~/.config/predd").expanduser()
CONFIG_FILE = CONFIG_DIR / "config.toml"
STATE_FILE = CONFIG_DIR / "state.json"
PID_FILE = CONFIG_DIR / "pid"
LOG_FILE = CONFIG_DIR / "log.txt"

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

DEFAULT_CONFIG_TEMPLATE = """\
# Repos to watch. Format: "owner/name"
repos = [
  "owner/repo",
]

# Repos only watched by predd (not hunter)
predd_only_repos = []

# Repos only watched by hunter (not predd)
hunter_only_repos = []

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

# Backend to use for reviews: "devin" or "claude"
backend = "devin"

# Model passed to the backend
# devin default: haiku-4-5
# claude default: claude-opus-4-7
model = "haiku-4-5"

# Branch prefix for hunter-created branches
branch_prefix = "usr/at"

# Maximum self-review/fix loops before flagging for human
max_review_fix_loops = 1

# Whether to self-review draft implementation PRs (false = wait for ready)
auto_review_draft = false
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
# Config
# ---------------------------------------------------------------------------

class Config:
    def __init__(self, data: dict):
        self.repos: list[str] = data["repos"]
        self.predd_only_repos: list[str] = data.get("predd_only_repos", [])
        self.hunter_only_repos: list[str] = data.get("hunter_only_repos", [])
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
            "haiku-4-5" if data.get("backend", "devin") == "devin" else "claude-opus-4-7"
        )
        self.branch_prefix: str = data.get("branch_prefix", "usr/at")
        self.max_review_fix_loops: int = data.get("max_review_fix_loops", 1)
        self.auto_review_draft: bool = data.get("auto_review_draft", False)
        self.max_resume_retries: int = data.get("max_resume_retries", 2)

    def to_dict(self) -> dict:
        return {
            "repos": self.repos,
            "predd_only_repos": self.predd_only_repos,
            "hunter_only_repos": self.hunter_only_repos,
            "poll_interval": self.poll_interval,
            "sound_new_pr": self.sound_new_pr,
            "sound_review_ready": self.sound_review_ready,
            "worktree_base": str(self.worktree_base),
            "github_user": self.github_user,
            "skill_path": str(self.skill_path),
            "proposal_skill_path": str(self.proposal_skill_path),
            "impl_skill_path": str(self.impl_skill_path),
            "trigger": self.trigger,
            "backend": self.backend,
            "model": self.model,
            "branch_prefix": self.branch_prefix,
            "max_review_fix_loops": self.max_review_fix_loops,
            "auto_review_draft": self.auto_review_draft,
            "max_resume_retries": self.max_resume_retries,
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


def state_key(repo: str, pr_number: int, head_sha: str) -> str:
    return f"{repo}#{pr_number}"


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
    else:
        beep_cmd = f"(New-Object Media.SoundPlayer '{sound_path}').PlaySync()"
    try:
        subprocess.run(
            [_PWSH, "-NoProfile", "-Command", beep_cmd],
            check=False, capture_output=True, timeout=10,
        )
    except Exception:
        pass


def notify_toast(title: str, body: str) -> None:
    if not _PWSH:
        return
    title_esc = title.replace("'", "\\'")
    body_esc = body.replace("'", "\\'")
    try:
        subprocess.run(
            [_PWSH, "-NoProfile", "-Command",
             f"New-BurntToastNotification -Text '{title_esc}', '{body_esc}'"],
            check=False, capture_output=True, timeout=15,
        )
    except Exception:
        pass

# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def gh_run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        check=check,
    )


def gh_list_open_prs(repo: str) -> list[dict]:
    """Return list of open, non-draft PRs not authored by the current user."""
    result = gh_run([
        "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number,title,author,headRefName,headRefOid,isDraft,reviewRequests",
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
                if repo_name in result.stdout:
                    return candidate
            except subprocess.CalledProcessError:
                pass
    return None


def setup_worktree(cfg: Config, repo: str, pr_number: int, head_sha: str, head_ref: str) -> Path:
    wt_path = worktree_path(cfg, repo, pr_number, head_sha)
    cfg.worktree_base.mkdir(parents=True, exist_ok=True)

    local_repo = find_local_repo(repo)
    if local_repo:
        # Fetch the PR branch then add worktree
        subprocess.run(
            ["git", "fetch", "origin", head_ref],
            cwd=local_repo, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), f"origin/{head_ref}"],
            cwd=local_repo, check=True, capture_output=True,
        )
    else:
        # Fall back: use gh pr checkout in a temp dir, then move
        tmp_dir = cfg.worktree_base / f"_tmp-{repo_slug(repo)}-{pr_number}"
        # Clean up any leftover tmp or target dirs from previous killed runs
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


def remove_worktree(worktree: str | Path) -> None:
    wt = Path(worktree)
    # Find the git dir to run worktree remove from
    try:
        # Walk up to find parent repo
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=wt, capture_output=True, text=True, check=True,
        )
        git_common = Path(result.stdout.strip())
        repo_root = git_common.parent if git_common.name == ".git" else git_common.parent.parent
        subprocess.run(
            ["git", "worktree", "remove", str(wt), "--force"],
            cwd=repo_root, check=True, capture_output=True,
        )
    except Exception:
        # If we can't use git worktree remove, just delete the directory
        import shutil
        shutil.rmtree(wt, ignore_errors=True)

# ---------------------------------------------------------------------------
# Review pipeline
# ---------------------------------------------------------------------------

_DEVIN_STRIP_ENV = {
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
}


def _load_skill(cfg: Config, pr_number: int) -> str:
    if not cfg.skill_path.exists():
        raise FileNotFoundError(f"Skill not found: {cfg.skill_path}")
    return cfg.skill_path.read_text().replace("$ARGUMENTS", str(pr_number))


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
        stdout, _ = proc.communicate(input=stdin_text, timeout=900)
    finally:
        _active_proc = None
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)
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
    (skill_dir / "pr-review.md").write_text(cfg.skill_path.read_text())
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
    raise ValueError(f"Unknown backend '{cfg.backend}'. Valid values: claude, devin")

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
    update_pr_state(state, key, head_sha=head_sha, status="reviewing",
                    first_seen=_now_iso())

    try:
        notify_sound(cfg.sound_new_pr)
        notify_toast("New PR", f"{key} — {title}")

        worktree = setup_worktree(cfg, repo, pr_number, head_sha, head_ref)
        logger.info("Worktree at %s", worktree)

        review_text = run_review(cfg, repo, pr_number, worktree)

        # Skill posts directly to GitHub; save output for reference
        summary_path = worktree / "review-summary.md"
        summary_path.write_text(review_text)

        update_pr_state(state, key,
                        status="submitted",
                        worktree=str(worktree),
                        draft_path=str(summary_path),
                        review_completed=_now_iso())

        notify_sound(cfg.sound_review_ready)
        notify_toast("Review posted", f"{key} — run `predd show {pr_number}`")
        logger.info("Review posted for %s", key)

    except Exception as e:
        logger.error("Failed processing %s: %s", key, e, exc_info=True)
        update_pr_state(state, key, status="failed")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

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
# CLI
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """predd: daemon that drafts PR reviews for human approval."""
    pass


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

    try:
        while not _stop.is_set():
            state = load_state()
            for repo in cfg.repos:
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
                    if pr["author"]["login"] == cfg.github_user:
                        continue
                    if pr["isDraft"]:
                        continue
                    if cfg.trigger == "requested":
                        requested = [r.get("login") for r in pr.get("reviewRequests", [])]
                        if cfg.github_user not in requested:
                            continue

                    pr_number = pr["number"]
                    head_sha = pr["headRefOid"]
                    key = f"{repo}#{pr_number}"

                    entry = state.get(key, {})
                    entry_sha = entry.get("head_sha", "")
                    entry_status = entry.get("status", "")

                    if entry_sha == head_sha and entry_status in (
                        "submitted", "rejected", "awaiting_approval", "reviewing"
                    ):
                        continue

                    if gh_pr_already_reviewed(repo, pr_number, cfg.github_user):
                        logger.info("Skipping %s — already reviewed or closed", key)
                        update_pr_state(state, key, head_sha=head_sha, status="rejected")
                        continue

                    _current_pr_key[:] = [key]
                    process_pr(cfg, state, repo, pr)
                    _current_pr_key.clear()

            if once or _stop.wait(cfg.poll_interval):
                break

        logger.info("Shutting down cleanly.")
    finally:
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


@cli.command(name="config")
def config_cmd():
    """Print resolved config."""
    cfg = load_config()
    # Print as TOML-ish key = value
    d = cfg.to_dict()
    lines = []
    for k, v in d.items():
        if isinstance(v, list):
            items = ", ".join(f'"{x}"' for x in v)
            lines.append(f"{k} = [{items}]")
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        else:
            lines.append(f"{k} = {v}")
    click.echo("\n".join(lines))


if __name__ == "__main__":
    cli()
