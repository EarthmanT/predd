#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "click",
# ]
# ///

import importlib.util
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import click

# ---------------------------------------------------------------------------
# Import shared pieces from predd
# ---------------------------------------------------------------------------

_predd_spec = importlib.util.spec_from_file_location(
    "predd", Path(__file__).resolve().parent / "predd.py"
)
_predd = importlib.util.module_from_spec(_predd_spec)
_predd_spec.loader.exec_module(_predd)

Config = _predd.Config
load_config = _predd.load_config
load_state = _predd.load_state
save_state = _predd.save_state
notify_sound = _predd.notify_sound
notify_toast = _predd.notify_toast
_run_proc = _predd._run_proc
_PWSH = _predd._PWSH
_DEVIN_STRIP_ENV = _predd._DEVIN_STRIP_ENV
repo_slug = _predd.repo_slug
find_local_repo = _predd.find_local_repo
setup_new_branch_worktree = _predd.setup_new_branch_worktree
_now_iso = _predd._now_iso
start_status_server = _predd.start_status_server
stop_status_server = _predd.stop_status_server
_run_bedrock_skill = _predd._run_bedrock_skill
gh_issue_comment = _predd.gh_issue_comment
gh_issue_add_label = _predd.gh_issue_add_label
gh_run = _predd.gh_run

# Failure comment templates for hunter
_HUNTER_NO_COMMITS_COMMENT = """\
⚠️ Hunter could not create a PR for this issue.

The AI skill ran but produced no git commits. This usually means:
- The issue description is too vague for the AI to understand what to build
- The issue requires context not present in the description
- The skill prompt needs improvement for this type of work

**Issue:** {repo}#{issue_number}
**Skill:** {skill}
**Error:** Skill produced no commits — not creating empty PR

Please either:
1. Add more details to the issue description
2. Create the PR manually
3. Improve the skill prompt at ~/.windsurf/skills/{skill}/SKILL.md
"""

_HUNTER_PUSH_FAILURE_COMMENT = """\
⚠️ Hunter could not push the PR branch for this issue.

The AI skill produced commits, but git push failed. This usually means:
- Branch protection rules blocking direct pushes
- Branch already exists remotely with conflicts
- Git authentication or permissions issue

**Issue:** {repo}#{issue_number}
**Branch:** {branch}
**Error:** {error}

The worktree with commits is preserved at:
{worktree_path}

Please either:
1. Fix branch protection rules to allow pushes from hunter
2. Manually push from the worktree and create the PR
3. Delete the worktree if the work should be discarded
"""

_HUNTER_CRASH_COMMENT = """\
⚠️ Hunter crashed while processing this issue.

The AI skill subprocess exited unexpectedly.

**Issue:** {repo}#{issue_number}
**Skill:** {skill}
**Error:** {error}

The worktree is preserved at:
{worktree_path}

Please check the logs for details:
tail -f ~/.config/predd/hunter-log.txt
"""

# ---------------------------------------------------------------------------
# Hunter-local subprocess runner — tracks _active_proc_hunter for shutdown
# ---------------------------------------------------------------------------

def _run_proc_hunter(cmd: list[str], worktree: Path, env: dict | None = None,
                     stdin_text: str | None = None) -> str:
    global _active_proc_hunter
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        text=True,
        cwd=str(worktree),
        env=env,
    )
    _active_proc_hunter = proc
    try:
        stdout, stderr_out = proc.communicate(input=stdin_text, timeout=900)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        _active_proc_hunter = None
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr_out)
    return stdout


def _run_claude(cfg: Config, prompt: str, worktree: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    return _run_proc_hunter(
        ["claude", "-p", "--dangerously-skip-permissions", "--model", cfg.model],
        worktree, env=env, stdin_text=prompt,
    )


def _run_devin_skill(cfg: Config, prompt: str, skill_path: Path, worktree: Path) -> str:
    """Run a skill via Devin, placing skill file in .devin/skills/."""
    skill_dir = worktree / ".devin" / "skills"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_name = skill_path.stem.lower()
    (skill_dir / f"{skill_name}.md").write_text(skill_path.read_text())
    env = {k: v for k, v in os.environ.items() if k not in _DEVIN_STRIP_ENV}
    return _run_proc_hunter(
        ["setsid", "devin", "-p", "--permission-mode", "auto",
         "--model", cfg.model, "--", prompt],
        worktree,
        env=env,
    )

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_DIR = Path("~/.config/predd").expanduser()
HUNTER_STATE_FILE = CONFIG_DIR / "hunter-state.json"
HUNTER_PID_FILE = CONFIG_DIR / "hunter-pid"
HUNTER_LOG_FILE = CONFIG_DIR / "hunter-log.txt"
HUNTER_DECISION_LOG = CONFIG_DIR / "hunter-decisions.jsonl"

JIRA_KEY_RE = re.compile(r"\[([A-Z][A-Z0-9]+-\d+)\]")


def extract_jira_key(title: str) -> str | None:
    """Extract Jira key from issue title, e.g. '[DAP09A-1184] foo' -> 'DAP09A-1184'."""
    m = JIRA_KEY_RE.search(title or "")
    return m.group(1) if m else None


def issue_identifier(issue_number: int, title: str) -> str:
    """Return Jira key from title if present, else GitHub issue number as string."""
    return extract_jira_key(title) or str(issue_number)


def label_jira_issue(repo: str, issue_number: int, title: str) -> None:
    """Apply 'jira' label to an issue if its title contains a Jira key."""
    jira_key = extract_jira_key(title)
    if not jira_key:
        return
    try:
        gh_ensure_label_exists(repo, "jira", color="0052CC")
        gh_issue_add_label(repo, issue_number, "jira")
        log_decision("jira_label_applied", repo=repo, issue=issue_number, jira_key=jira_key)
    except Exception as e:
        logger.warning("Could not apply jira label to %s#%d: %s", repo, issue_number, e)


def _sweep_jira_labels(cfg: Config, repos: list[str]) -> None:
    """One-shot startup sweep: label open issues with Jira keys in their titles."""
    for repo in repos:
        try:
            result = gh_run([
                "issue", "list", "--repo", repo,
                "--state", "open", "--limit", "100",
                "--json", "number,title,labels",
            ])
            issues = json.loads(result.stdout)
        except Exception as e:
            logger.warning("Jira label sweep: failed to list issues for %s: %s", repo, e)
            continue

        for issue in issues:
            title = issue.get("title", "")
            if not extract_jira_key(title):
                continue
            label_names = [lbl["name"] for lbl in issue.get("labels", [])]
            if "jira" in label_names:
                continue
            label_jira_issue(repo, issue["number"], title)

    logger.info("Jira label sweep complete")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging() -> logging.Logger:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("hunter")
    logger.setLevel(logging.DEBUG)
    handler = logging.handlers.RotatingFileHandler(
        HUNTER_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(stderr_handler)
    return logger


logger = logging.getLogger("hunter")

# ---------------------------------------------------------------------------
# Hunter state helpers
# ---------------------------------------------------------------------------


def load_hunter_state() -> dict:
    if not HUNTER_STATE_FILE.exists():
        return {}
    try:
        return json.loads(HUNTER_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to read hunter state file; starting fresh")
        return {}


def save_hunter_state(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HUNTER_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(HUNTER_STATE_FILE)


def update_issue_state(state: dict, key: str, **fields) -> None:
    if key not in state:
        state[key] = {}
    state[key].update(fields)
    save_hunter_state(state)


def log_decision(event: str, **fields) -> None:
    """Append a structured decision record to hunter-decisions.jsonl."""
    record = {"ts": _now_iso(), "event": event, **fields}
    try:
        with open(HUNTER_DECISION_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


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
    if HUNTER_PID_FILE.exists():
        try:
            pid = int(HUNTER_PID_FILE.read_text().strip())
            if _pid_alive(pid):
                click.echo(f"hunter already running (PID {pid}). Exiting.", err=True)
                sys.exit(1)
        except ValueError:
            pass
    HUNTER_PID_FILE.write_text(str(os.getpid()))


def release_pid_file() -> None:
    try:
        HUNTER_PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_active_proc_hunter: subprocess.Popen | None = None
_stop = threading.Event()
_current_issue_key: list[str] = []


def _shutdown(signum, frame):
    if _stop.is_set():
        # Second signal — force quit
        key = _current_issue_key[0] if _current_issue_key else None
        if _active_proc_hunter is not None:
            logger.warning("Force quit — killing skill subprocess")
            _active_proc_hunter.terminate()
        if key:
            logger.warning("Force quit — rolling back %s to unprocessed", key)
            state = load_hunter_state()
            state.pop(key, None)
            save_hunter_state(state)
        release_pid_file()
        sys.exit(1)
    _stop.set()
    if _active_proc_hunter is not None:
        logger.info("Finishing current task before exiting (^C again to force quit)...")


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
            result.check_returncode()
        elif any(x in stderr for x in _TRANSIENT_ERRORS):
            wait = 2 ** attempt * 5
            logger.warning("gh transient error (attempt %d), retrying in %ds: %s",
                           attempt + 1, wait, result.stderr.strip())
            time.sleep(wait)
        else:
            result.check_returncode()
    if check:
        result.check_returncode()
    return result


def gh_list_assigned_issues(repo: str) -> list[dict]:
    """List open issues assigned to @me in repo."""
    result = gh_run([
        "issue", "list",
        "--assignee", "@me",
        "--repo", repo,
        "--state", "open",
        "--json", "number,title,author,labels,body",
        "--limit", "100",
    ])
    return json.loads(result.stdout)


def gh_issue_add_label(repo: str, issue_number: int, label: str) -> None:
    gh_run(["issue", "edit", str(issue_number), "--repo", repo, "--add-label", label])


def gh_issue_remove_label(repo: str, issue_number: int, label: str) -> None:
    gh_run(["issue", "edit", str(issue_number), "--repo", repo, "--remove-label", label], check=False)


def gh_issue_view(repo: str, issue_number: int) -> dict:
    result = gh_run([
        "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "number,title,author,labels,body,assignees",
    ])
    return json.loads(result.stdout)


def gh_issue_comment(repo: str, issue_number: int, body: str) -> None:
    gh_run(["issue", "comment", str(issue_number), "--repo", repo, "--body", body])


def gh_ensure_label_exists(repo: str, label: str, color: str = "0075ca") -> None:
    gh_run(["label", "create", label, "--repo", repo, "--color", color, "--force"])


def gh_find_merged_proposal(repo: str, issue_number: int, title: str) -> int | None:
    """Find a merged sdd-proposal PR for this issue. Returns PR number or None."""
    result = gh_run([
        "pr", "list",
        "--repo", repo,
        "--state", "merged",
        "--label", "sdd-proposal",
        "--json", "number,title,body",
        "--limit", "100",
    ], check=False)
    if result.returncode != 0:
        return None
    prs = json.loads(result.stdout)
    pattern = re.compile(rf"#{issue_number}\b")
    for pr in prs:
        body = pr.get("body") or ""
        pr_title = pr.get("title") or ""
        if pattern.search(body) or pattern.search(pr_title):
            return pr["number"]
    return None


def gh_create_branch_and_pr(
    repo: str, base: str, branch: str, title: str, body: str, draft: bool = True,
    worktree: Path | None = None, label: str | None = None,
) -> int:
    """Create branch, push it, create PR. Returns PR number."""
    cwd = str(worktree) if worktree else None

    # Check current branch; if already on target branch, nothing to do
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, cwd=cwd,
    )
    current_branch = current.stdout.strip()
    if current_branch != branch:
        co = subprocess.run(
            ["git", "checkout", branch],
            capture_output=True, cwd=cwd,
        )
        if co.returncode != 0:
            subprocess.run(
                ["git", "checkout", "-b", branch],
                check=True, capture_output=True, cwd=cwd,
            )
    subprocess.run(
        ["git", "push", "-u", "origin", branch],
        check=True, capture_output=True, cwd=cwd,
    )

    cmd = [
        "pr", "create",
        "--repo", repo,
        "--base", base,
        "--head", branch,
        "--title", title,
        "--body", body,
    ]
    if draft:
        cmd.append("--draft")
    if label:
        cmd += ["--label", label]

    result = gh_run(cmd)
    # Output is the PR URL; parse number from it
    pr_url = result.stdout.strip()
    m = re.search(r"/pull/(\d+)", pr_url)
    if not m:
        raise ValueError(f"Could not parse PR number from: {pr_url}")
    return int(m.group(1))


def gh_list_prs_with_marker(repo: str, marker: str) -> list[dict]:
    """List open PRs whose body contains marker."""
    result = gh_run([
        "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number,title,body,isDraft,headRefName,headRefOid,state",
        "--limit", "100",
    ])
    prs = json.loads(result.stdout)
    return [pr for pr in prs if marker in (pr.get("body") or "")]


def gh_pr_is_merged(repo: str, pr_number: int) -> bool:
    result = gh_run([
        "pr", "view", str(pr_number),
        "--repo", repo,
        "--json", "state",
    ])
    data = json.loads(result.stdout)
    return data.get("state") == "MERGED"


def gh_pr_is_draft(repo: str, pr_number: int) -> bool:
    result = gh_run([
        "pr", "view", str(pr_number),
        "--repo", repo,
        "--json", "isDraft",
    ])
    data = json.loads(result.stdout)
    return bool(data.get("isDraft"))


def gh_pr_mark_ready(repo: str, pr_number: int) -> None:
    gh_run(["pr", "ready", str(pr_number), "--repo", repo])


def gh_issue_is_closed(repo: str, issue_number: int) -> bool:
    """Return True if the issue is in CLOSED state on GitHub."""
    result = gh_run(["issue", "view", str(issue_number), "--repo", repo,
                     "--json", "state"], check=False)
    if result.returncode != 0:
        return False
    try:
        return json.loads(result.stdout).get("state") == "CLOSED"
    except (json.JSONDecodeError, AttributeError):
        return False


def gh_issue_reopen_and_reassign(
    repo: str, issue_number: int, assignee: str, comment: str
) -> None:
    gh_run(["issue", "reopen", str(issue_number), "--repo", repo])
    gh_run(["issue", "edit", str(issue_number), "--repo", repo, "--assignee", assignee])
    gh_run(["issue", "comment", str(issue_number), "--repo", repo, "--body", comment])


def gh_repo_default_branch(repo: str) -> str:
    """Return the default branch name for a repo."""
    result = gh_run(["repo", "view", repo, "--json", "defaultBranchRef"])
    data = json.loads(result.stdout)
    return data["defaultBranchRef"]["name"]


def gh_pr_reviews(repo: str, pr_number: int) -> list[dict]:
    """Fetch formal reviews on a PR (APPROVED/REQUEST_CHANGES/COMMENT)."""
    owner, name = repo.split("/")
    result = gh_run(["api", f"repos/{owner}/{name}/pulls/{pr_number}/reviews"], check=False)
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def gh_pr_inline_comments(repo: str, pr_number: int) -> list[dict]:
    """Fetch inline review comments on a PR."""
    owner, name = repo.split("/")
    result = gh_run(["api", f"repos/{owner}/{name}/pulls/{pr_number}/comments"], check=False)
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def gh_pr_issue_comments(repo: str, pr_number: int) -> list[dict]:
    """Fetch general conversation comments on a PR."""
    owner, name = repo.split("/")
    result = gh_run(["api", f"repos/{owner}/{name}/issues/{pr_number}/comments"], check=False)
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
# Race condition prevention
# ---------------------------------------------------------------------------


def try_claim_issue(cfg: Config, repo: str, issue_number: int) -> bool:
    """Attempt to claim an issue using label-then-verify pattern."""
    label = f"{cfg.github_user}:in-progress"
    try:
        gh_ensure_label_exists(repo, label)
        gh_issue_add_label(repo, issue_number, label)
        time.sleep(2)
        issue = gh_issue_view(repo, issue_number)
        label_names = [lbl["name"] for lbl in issue.get("labels", [])]
        if label not in label_names:
            return False
        # Check for competing hunter labels from other users (hunter-style labels)
        competing = [
            lbl for lbl in label_names
            if lbl.endswith(":in-progress") and lbl != label
        ]
        if competing:
            return False
        return True
    except Exception as e:
        logger.warning("try_claim_issue failed for %s#%d: %s", repo, issue_number, e)
        log_decision("claim_failed", repo=repo, issue=issue_number, reason="label_error")
        return False


# ---------------------------------------------------------------------------
# Branch/slug helpers
# ---------------------------------------------------------------------------


def issue_slug(title: str, max_len: int = 30) -> str:
    """Lowercase title, replace non-alphanumeric with hyphens, truncate."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_len].rstrip("-")


def proposal_branch(cfg: Config, issue_number: int, title: str) -> str:
    return f"{cfg.branch_prefix}/{issue_identifier(issue_number, title)}-proposal-{issue_slug(title)}"


def impl_branch(cfg: Config, issue_number: int, title: str) -> str:
    return f"{cfg.branch_prefix}/{issue_identifier(issue_number, title)}-impl-{issue_slug(title)}"


# ---------------------------------------------------------------------------
# Skill runner
# ---------------------------------------------------------------------------


def build_issue_context(issue_number: int, title: str, body: str, entry: dict) -> str:
    """Build a rich context string to pass as $ARGUMENTS to proposal/impl skills."""
    lines = [f"Issue #{issue_number}: {title}", ""]
    for field in ("type", "epic", "sprint", "capability"):
        val = entry.get(field)
        if val:
            lines.append(f"{field.capitalize()}: {val}")
    lines += ["", "Description:", body or "(no description)"]
    return "\n".join(lines)


def commit_skill_output(worktree: Path, message: str) -> bool:
    """Stage all changes and commit. Returns True if there was anything to commit."""
    # Ensure sensitive files are not staged
    gitignore = worktree / ".gitignore"
    ignore_patterns = "\n.env\n.env.*\n*.key\n*.pem\n.devin/\n.hunter-prompt.txt\n"
    if gitignore.exists():
        existing = gitignore.read_text()
        if ".devin/" not in existing:
            gitignore.write_text(existing + ignore_patterns)
    else:
        gitignore.write_text(ignore_patterns)
    subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
    result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(worktree), capture_output=True, text=True,
    )
    return result.returncode == 0


def skill_has_commits(worktree: Path) -> bool:
    """Return True if the worktree has new commits or uncommitted changes since branch base."""
    # Check for uncommitted changes (staged or unstaged)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=str(worktree),
    )
    if status.stdout.strip():
        return True
    # Check for commits not on remote (unpushed)
    unpushed = subprocess.run(
        ["git", "log", "--oneline", "HEAD", "--not", "--remotes"],
        capture_output=True, text=True, cwd=str(worktree),
    )
    return bool(unpushed.stdout.strip())


def run_skill(cfg: Config, skill_path: Path, arguments: str, worktree: Path) -> str:
    """Load skill and run with arguments as the task context."""
    if not skill_path.exists():
        raise FileNotFoundError(f"Skill not found: {skill_path}")
    skill_text = skill_path.read_text()
    # Strip YAML frontmatter if present
    if skill_text.startswith("---"):
        parts = skill_text.split("---", 2)
        skill_body = parts[2].lstrip("\n") if len(parts) >= 3 else skill_text
    else:
        skill_body = skill_text
    # Task-first structure: give the concrete task before the skill instructions
    # so the model doesn't wait for user input
    prompt = f"Run the following workflow for this task:\n\n{arguments}\n\n---\n\n{skill_body}"
    if cfg.backend == "claude":
        return _run_claude(cfg, prompt, worktree)
    if cfg.backend == "devin":
        return _run_devin_skill(cfg, prompt, skill_path, worktree)
    if cfg.backend == "bedrock":
        return _run_bedrock_skill(cfg, prompt, skill_path, worktree)
    raise ValueError(f"Unknown backend: {cfg.backend}")


# ---------------------------------------------------------------------------
# Core processing functions
# ---------------------------------------------------------------------------


def process_issue(cfg: Config, state: dict, repo: str, issue: dict) -> None:
    """Pick up a new issue: claim it, create proposal PR."""
    issue_number = issue["number"]
    title = issue["title"]
    key = f"{repo}!{issue_number}"

    logger.info("Picking up issue %s: %s", key, title)

    # Apply jira label if title contains a Jira key
    label_jira_issue(repo, issue_number, title)

    if not try_claim_issue(cfg, repo, issue_number):
        logger.info("Could not claim issue %s — skipping", key)
        log_decision("claim_failed", repo=repo, issue=issue_number, reason="try_claim_returned_false")
        return

    try:
        base_branch = gh_repo_default_branch(repo)
    except Exception as e:
        logger.error("Failed to get default branch for %s: %s", repo, e)
        return

    branch = proposal_branch(cfg, issue_number, title)
    worktree = cfg.worktree_base / f"{repo_slug(repo)}-{branch.replace('/', '-')}"

    issue_body = issue.get("body") or ""

    update_issue_state(state, key,
                       status="in_progress",
                       issue_number=issue_number,
                       repo=repo,
                       title=title,
                       issue_body=issue_body,
                       issue_author=issue.get("author", {}).get("login", ""),
                       base_branch=base_branch,
                       proposal_branch=branch,
                       proposal_worktree=str(worktree),
                       resume_attempts=0,
                       first_seen=_now_iso())

    try:
        notify_sound(cfg.sound_new_pr)
        notify_toast("New issue picked up", f"{key} — {title}")

        worktree = setup_new_branch_worktree(cfg, repo, branch, base_branch)
        logger.info("Proposal worktree at %s", worktree)

        context = build_issue_context(
            issue_number, title,
            state.get(key, {}).get("issue_body", ""),
            state.get(key, {}),
        )
        run_skill(cfg, cfg.proposal_skill_path, context, worktree)
        commit_skill_output(worktree, f"proposal: issue #{issue_number} — {title[:60]}")

        if not skill_has_commits(worktree):
            log_decision("skill_no_commits", repo=repo, issue=issue_number, skill="proposal")
            raise RuntimeError("Proposal skill produced no commits — not creating empty PR")

        pr_body = f"Proposal for issue #{issue_number}"
        gh_ensure_label_exists(repo, "sdd-proposal", color="0075ca")
        pr_number = gh_create_branch_and_pr(
            repo=repo,
            base=base_branch,
            branch=branch,
            title=f"Proposal: {title}",
            body=pr_body,
            draft=True,
            worktree=worktree,
            label="sdd-proposal",
        )

        in_progress_label = f"{cfg.github_user}:in-progress"
        proposal_label = f"{cfg.github_user}:proposal-open"
        gh_ensure_label_exists(repo, proposal_label)
        gh_issue_add_label(repo, issue_number, proposal_label)
        gh_issue_remove_label(repo, issue_number, in_progress_label)

        update_issue_state(state, key,
                           status="proposal_open",
                           proposal_pr=pr_number,
                           proposal_branch=branch,
                           proposal_worktree=str(worktree))
        log_decision("issue_pickup", repo=repo, issue=issue_number, title=title)
        log_decision("proposal_created", repo=repo, issue=issue_number, pr=pr_number)
        logger.info("Proposal PR #%d created for issue %s", pr_number, key)

    except RuntimeError as e:
        # Specific errors like "no commits" should be commented
        logger.error("Failed processing issue %s: %s", key, e, exc_info=True)
        if cfg.comment_on_failures:
            try:
                if "no commits" in str(e).lower():
                    comment = _HUNTER_NO_COMMITS_COMMENT.format(
                        repo=repo, issue_number=issue_number, skill="proposal"
                    )
                else:
                    comment = _HUNTER_CRASH_COMMENT.format(
                        repo=repo, issue_number=issue_number, skill="proposal",
                        error=str(e), worktree_path=worktree
                    )
                gh_issue_comment(repo, issue_number, comment)
                failure_label = f"{cfg.github_user}:hunter-failed"
                gh_issue_add_label(repo, issue_number, failure_label)
                log_decision("issue_failure_commented", repo=repo, issue=issue_number, reason=str(e)[:100])
            except Exception as comment_err:
                logger.error("Failed to post failure comment for %s: %s", key, comment_err)
        update_issue_state(state, key, status="failed")
    except Exception as e:
        logger.error("Failed processing issue %s: %s", key, e, exc_info=True)
        if cfg.comment_on_failures:
            try:
                comment = _HUNTER_CRASH_COMMENT.format(
                    repo=repo, issue_number=issue_number, skill="proposal",
                    error=str(e), worktree_path=worktree
                )
                gh_issue_comment(repo, issue_number, comment)
                failure_label = f"{cfg.github_user}:hunter-failed"
                gh_issue_add_label(repo, issue_number, failure_label)
                log_decision("issue_failure_commented", repo=repo, issue=issue_number, reason="crash", error=str(e)[:100])
            except Exception as comment_err:
                logger.error("Failed to post failure comment for %s: %s", key, comment_err)
        update_issue_state(state, key, status="failed")


def collect_pr_feedback(cfg: Config, state: dict, repo: str, key: str,
                        pr_number: int, feedback_field: str) -> None:
    """Collect review feedback from a PR and store in hunter state and decision log."""
    if not cfg.collect_pr_feedback:
        return

    entry = state.get(key, {})
    existing = entry.get(feedback_field, [])
    # Track which review IDs we've already collected to avoid duplicates
    seen_ids = {f.get("review_id") for f in existing if f.get("review_id")}

    try:
        reviews = gh_pr_reviews(repo, pr_number)
        inline_comments = gh_pr_inline_comments(repo, pr_number)
        issue_comments = gh_pr_issue_comments(repo, pr_number)
    except Exception as e:
        logger.warning("collect_pr_feedback: failed to fetch feedback for PR %d: %s", pr_number, e)
        return

    # Group inline comments by review ID
    inline_by_review: dict[int, list[dict]] = {}
    for c in inline_comments:
        rid = c.get("pull_request_review_id")
        if rid:
            inline_by_review.setdefault(rid, []).append({
                "path": c.get("path", ""),
                "line": c.get("line") or c.get("original_line"),
                "body": c.get("body", ""),
            })

    new_feedback = []
    for review in reviews:
        review_id = review.get("id")
        if review_id in seen_ids:
            continue
        state_val = review.get("state", "COMMENTED")
        reviewer = (review.get("user") or {}).get("login", "")
        body = review.get("body") or ""
        submitted_at = review.get("submitted_at", _now_iso())

        record = {
            "review_id": review_id,
            "ts": submitted_at,
            "reviewer": reviewer,
            "type": state_val,
            "body": body,
            "inline_comments": inline_by_review.get(review_id, []),
        }
        new_feedback.append(record)
        log_decision("pr_feedback",
                     repo=repo,
                     issue=entry.get("issue_number"),
                     pr=pr_number,
                     type=state_val,
                     reviewer=reviewer,
                     body=body[:200])  # truncate for log

    # Add general issue comments not tied to a review (new ones only)
    seen_comment_ids = {f.get("comment_id") for f in existing if f.get("comment_id")}
    for c in issue_comments:
        cid = c.get("id")
        if cid in seen_comment_ids:
            continue
        author = (c.get("user") or {}).get("login", "")
        if author == cfg.github_user:
            continue  # skip our own comments
        record = {
            "comment_id": cid,
            "ts": c.get("created_at", _now_iso()),
            "reviewer": author,
            "type": "COMMENT",
            "body": c.get("body", ""),
            "inline_comments": [],
        }
        new_feedback.append(record)
        log_decision("pr_feedback",
                     repo=repo,
                     issue=entry.get("issue_number"),
                     pr=pr_number,
                     type="COMMENT",
                     reviewer=author,
                     body=(c.get("body") or "")[:200])

    if new_feedback:
        all_feedback = existing + new_feedback
        update_issue_state(state, key, **{feedback_field: all_feedback})
        logger.info("Collected %d new feedback item(s) for %s PR #%d",
                    len(new_feedback), feedback_field.replace("_feedback", ""), pr_number)


def check_proposal_merged(cfg: Config, state: dict, repo: str, key: str, entry: dict) -> None:
    """If a merged sdd-proposal PR exists for this issue, kick off implementation."""
    issue_number = entry["issue_number"]
    title = entry["title"]

    try:
        if gh_issue_is_closed(repo, issue_number):
            logger.info("Issue %s was closed manually — stopping", key)
            log_decision("issue_skip", repo=repo, issue=issue_number, reason="closed_manually")
            update_issue_state(state, key, status="submitted")
            return
    except Exception:
        pass  # if we can't check, proceed normally

    # Collect feedback on the proposal PR while waiting for merge
    proposal_pr = entry.get("proposal_pr")
    if proposal_pr:
        collect_pr_feedback(cfg, state, repo, key, proposal_pr, "proposal_feedback")
        state = load_hunter_state()  # reload after potential state update
        entry = state.get(key, entry)

    merged_pr = gh_find_merged_proposal(repo, issue_number, title)
    if not merged_pr:
        return

    log_decision("proposal_merged", repo=repo, issue=issue_number, pr=merged_pr)
    logger.info("Proposal PR #%d merged for %s — starting implementation", merged_pr, key)

    update_issue_state(state, key, status="implementing")

    try:
        base_branch = gh_repo_default_branch(repo)
        branch = impl_branch(cfg, issue_number, title)
        worktree = setup_new_branch_worktree(cfg, repo, branch, base_branch)
        logger.info("Impl worktree at %s", worktree)

        context = build_issue_context(
            issue_number, title,
            entry.get("issue_body", ""),
            entry,
        )
        run_skill(cfg, cfg.impl_skill_path, context, worktree)
        commit_skill_output(worktree, f"impl: issue #{issue_number} — {title[:60]}")

        if not skill_has_commits(worktree):
            log_decision("skill_no_commits", repo=repo, issue=issue_number, skill="impl")
            raise RuntimeError("Impl skill produced no commits — not creating empty PR")

        pr_body = f"Implementation for issue #{issue_number}"
        gh_ensure_label_exists(repo, "sdd-implementation", color="e4e669")
        pr_number = gh_create_branch_and_pr(
            repo=repo,
            base=base_branch,
            branch=branch,
            title=f"Implement: {title}",
            body=pr_body,
            draft=True,
            worktree=worktree,
            label="sdd-implementation",
        )

        implementing_label = f"{cfg.github_user}:implementing"
        proposal_label = f"{cfg.github_user}:proposal-open"
        gh_ensure_label_exists(repo, implementing_label)
        gh_issue_add_label(repo, issue_number, implementing_label)
        gh_issue_remove_label(repo, issue_number, proposal_label)

        update_issue_state(state, key,
                           status="implementing",
                           impl_pr=pr_number,
                           impl_branch=branch,
                           impl_worktree=str(worktree))
        log_decision("impl_created", repo=repo, issue=issue_number, pr=pr_number)
        logger.info("Impl PR #%d created for issue %s", pr_number, key)

    except Exception as e:
        logger.error("Failed starting implementation for %s: %s", key, e, exc_info=True)
        update_issue_state(state, key, status="failed")


def self_review_loop(
    cfg: Config, state: dict, repo: str, key: str, entry: dict, worktree: Path
) -> None:
    """Review own PR, fix findings, up to max_review_fix_loops times."""
    issue_number = entry["issue_number"]
    impl_pr = entry["impl_pr"]
    loops_done = entry.get("review_loops_done", 0)

    if loops_done >= cfg.max_review_fix_loops:
        logger.info("Self-review loop exhausted for %s — flagging for human", key)
        msg = f"Self-review loop exhausted. Human review needed on PR #{impl_pr}."
        try:
            gh_issue_comment(repo, issue_number, msg)
        except Exception as e:
            logger.warning("Could not post exhaustion comment: %s", e)
        update_issue_state(state, key, status="ready_for_review")
        try:
            gh_pr_mark_ready(repo, impl_pr)
        except Exception as e:
            logger.warning("Could not mark PR ready: %s", e)
        return

    update_issue_state(state, key, status="self_reviewing")

    try:
        review_output = run_skill(cfg, cfg.skill_path, str(impl_pr), worktree)
    except Exception as e:
        logger.error("Self-review skill failed for %s: %s", key, e, exc_info=True)
        update_issue_state(state, key, status="failed")
        return

    if "REQUEST_CHANGES" not in review_output and "APPROVE" in review_output:
        logger.info("Self-review approved for %s — marking ready", key)
        update_issue_state(state, key, status="ready_for_review")
        try:
            gh_pr_mark_ready(repo, impl_pr)
        except Exception as e:
            logger.warning("Could not mark PR ready: %s", e)
        return

    # Need fixes — run impl skill again
    logger.info("Self-review found issues for %s — running fix loop %d", key, loops_done + 1)
    try:
        fix_prompt = f"Fix the review findings on PR #{impl_pr}. Original issue: #{issue_number}"
        if cfg.backend == "claude":
            _run_claude(cfg, fix_prompt, worktree)
        elif cfg.backend == "bedrock":
            _run_bedrock_skill(cfg, fix_prompt, cfg.impl_skill_path, worktree)
        else:
            _run_devin_skill(cfg, fix_prompt, cfg.impl_skill_path, worktree)
        # Push fixes
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(worktree), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", f"fix: address review findings (loop {loops_done + 1})"],
            cwd=str(worktree), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=str(worktree), check=True, capture_output=True,
        )
    except Exception as e:
        logger.error("Fix loop failed for %s: %s", key, e, exc_info=True)
        update_issue_state(state, key, status="failed")
        return

    update_issue_state(state, key,
                       status="implementing",
                       review_loops_done=loops_done + 1)


def check_impl_ready_for_review(
    cfg: Config, state: dict, repo: str, key: str, entry: dict
) -> None:
    """If impl PR is not draft (or auto_review_draft=True), run self-review."""
    issue_number = entry["issue_number"]
    try:
        if gh_issue_is_closed(repo, issue_number):
            logger.info("Issue %s was closed manually — stopping", key)
            log_decision("issue_skip", repo=repo, issue=issue_number, reason="closed_manually")
            update_issue_state(state, key, status="submitted")
            return
    except Exception:
        pass

    impl_pr = entry.get("impl_pr")
    if not impl_pr:
        return

    # Collect feedback on the impl PR
    if impl_pr:
        collect_pr_feedback(cfg, state, repo, key, impl_pr, "impl_feedback")
        state = load_hunter_state()
        entry = state.get(key, entry)

    try:
        is_draft = gh_pr_is_draft(repo, impl_pr)
    except Exception as e:
        logger.warning("Could not check draft status for PR %d: %s", impl_pr, e)
        return

    if is_draft and not cfg.auto_review_draft:
        return

    worktree_str = entry.get("impl_worktree")
    if not worktree_str or not Path(worktree_str).exists():
        logger.warning("Impl worktree missing for %s — resetting to re-run impl", key)
        update_issue_state(state, key, status="implementing", impl_pr=None, impl_worktree=None)
        return

    worktree = Path(worktree_str)
    self_review_loop(cfg, state, repo, key, entry, worktree)


def check_impl_merged(
    cfg: Config, state: dict, repo: str, key: str, entry: dict
) -> None:
    """If impl PR merged, close the issue."""
    impl_pr = entry.get("impl_pr")
    if not impl_pr:
        return

    issue_number = entry["issue_number"]
    try:
        if gh_issue_is_closed(repo, issue_number):
            logger.info("Issue %s was closed manually — stopping", key)
            log_decision("issue_skip", repo=repo, issue=issue_number, reason="closed_manually")
            update_issue_state(state, key, status="submitted")
            return
    except Exception:
        pass  # if we can't check, proceed normally

    try:
        if not gh_pr_is_merged(repo, impl_pr):
            return
    except Exception as e:
        logger.warning("Could not check impl PR %d for %s: %s", impl_pr, key, e)
        return
    logger.info("Impl PR #%d merged for %s — closing issue", impl_pr, key)

    try:
        # Remove hunter labels before closing
        for label_suffix in (":implementing", ":proposal-open", ":in-progress"):
            gh_issue_remove_label(repo, issue_number,
                                  f"{cfg.github_user}{label_suffix}")
        gh_issue_comment(repo, issue_number,
                         f"Implemented in #{impl_pr}. Closing.")
        gh_run(["issue", "close", str(issue_number), "--repo", repo])
        log_decision("issue_closed", repo=repo, issue=issue_number, pr=impl_pr)
        update_issue_state(state, key, status="submitted")
        logger.info("Issue %s closed", key)

    except Exception as e:
        logger.error("Failed closing issue %s: %s", key, e, exc_info=True)
        update_issue_state(state, key, status="failed")


# ---------------------------------------------------------------------------
# Resume and rollback
# ---------------------------------------------------------------------------

TERMINAL_STATES = {"merged", "awaiting_verification", "submitted", "failed"}
HUNTER_LABEL_PREFIXES = (":in-progress", ":proposal-open", ":implementing", ":awaiting-verification")


# ---------------------------------------------------------------------------
# Jira CSV ingestion
# ---------------------------------------------------------------------------

def _parse_csv_row(row: dict) -> dict:
    """Normalise a CSV row to lowercase keys."""
    return {(k or "").lower().strip(): (v or "").strip() for k, v in row.items()}


def _parse_capability(description: str) -> str | None:
    m = re.search(r"capability:\s*(\d+)\s+(.+)", description, re.IGNORECASE)
    if m:
        return f"{m.group(1)} — {m.group(2).strip()}"
    return None


EPIC_COLUMNS = (
    "epic link",
    "epic name",
    "custom field (epic link)",
    "custom field (epic name)",
    "parent",
    "parent key",
)

SPRINT_COLUMNS = (
    "sprint",
    "custom field (sprint)",
)


def _find_epic(row: dict) -> str:
    """Find Epic value from the first non-empty column in EPIC_COLUMNS."""
    for col in EPIC_COLUMNS:
        val = row.get(col, "")
        if val:
            return val
    return ""


def _find_sprint(row: dict) -> str:
    """Find Sprint value from the first non-empty column in SPRINT_COLUMNS."""
    for col in SPRINT_COLUMNS:
        val = row.get(col, "")
        if val:
            return val
    return ""


def _build_issue_body(row: dict, jira_base_url: str) -> tuple[str, list[str]]:
    """Build the GH issue body and return (body, missing_fields)."""
    jira_key = row.get("issue key", "")
    summary = row.get("summary", "")
    issue_type = row.get("issue type", "")
    epic = _find_epic(row)
    sprint = _find_sprint(row)
    description = row.get("description", "")
    capability = _parse_capability(description)

    missing = []
    if not epic:
        missing.append("Epic not set")
    if not capability:
        missing.append("No `capability: <id> <name>` line in description")

    lines = ["| Field | Value |", "|-------|-------|"]
    if jira_key:
        lines.append(f"| Jira | [{jira_key}]({jira_base_url}/browse/{jira_key}) |")
    if issue_type:
        lines.append(f"| Type | {issue_type} |")
    if epic:
        # Epic link is a key — hyperlink it; epic name is plain text
        if re.match(r"^[A-Z]+-\d+$", epic):
            lines.append(f"| Epic | [{epic}]({jira_base_url}/browse/{epic}) |")
        else:
            lines.append(f"| Epic | {epic} |")
    if sprint:
        lines.append(f"| Sprint | {sprint} |")
    if capability:
        lines.append(f"| Capability | {capability} |")

    body = "\n".join(lines)

    if missing:
        warning = "\n\n> ⚠️ **Missing required fields:**\n" + \
                  "".join(f"\n> - {m}" for m in missing)
        body += warning

    if description:
        body += f"\n\n---\n\n{description}"

    return body, missing


def gh_issue_exists(repo: str, jira_key: str) -> bool:
    """Return True if an open or closed GH issue with this Jira key already exists."""
    result = gh_run([
        "issue", "list",
        "--repo", repo,
        "--state", "all",
        "--search", f"[{jira_key}]",
        "--json", "number,title",
        "--limit", "10",
    ], check=False)
    if result.returncode != 0:
        return False
    issues = json.loads(result.stdout)
    return any(f"[{jira_key}]" in (i.get("title") or "") for i in issues)


def gh_issue_create(repo: str, title: str, body: str, assignee: str | None = None) -> int | None:
    """Create a GH issue. Returns issue number or None on failure."""
    cmd = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
    if assignee:
        cmd += ["--assignee", assignee]
    result = gh_run(cmd, check=False)
    if result.returncode != 0:
        return None
    m = re.search(r"/issues/(\d+)", result.stdout)
    return int(m.group(1)) if m else None


def ingest_jira_csv(cfg: Config, repos: list[str]) -> None:
    """Read CSV files from jira_csv_dir and create missing GH issues."""
    if not cfg.jira_csv_dir or not cfg.jira_csv_dir.exists():
        return

    csv_files = sorted(cfg.jira_csv_dir.glob("*.csv"))
    if not csv_files:
        return

    logger.info("CSV ingest: scanning %d file(s) in %s", len(csv_files), cfg.jira_csv_dir)

    for csv_file in csv_files:
        try:
            import csv as _csv
            with open(csv_file, newline="", encoding="utf-8-sig") as f:
                reader = _csv.reader(f)
                header = next(reader)
                rows = []
                for values in reader:
                    # Build dict keeping first non-empty value for duplicate keys
                    row: dict[str, str] = {}
                    for col, val in zip(header, values):
                        key = (col or "").lower().strip()
                        if key not in row or not row[key]:
                            row[key] = (val or "").strip()
                    rows.append(row)
        except Exception as e:
            logger.warning("CSV ingest: failed to read %s: %s", csv_file, e)
            continue

        if rows:
            logger.info("CSV ingest: %s columns: %s", csv_file.name, sorted(rows[0].keys()))

        for row in rows:
            jira_key = row.get("issue key", "").strip()
            summary = row.get("summary", "").strip()
            if not jira_key or not summary:
                continue

            issue_type = row.get("issue type", "").strip().lower()
            if issue_type in {t.lower() for t in cfg.skip_jira_issue_types}:
                log_decision(
                    "csv_issue_skip",
                    jira_key=jira_key,
                    reason="excluded_type",
                    issue_type=issue_type,
                )
                logger.info(
                    "CSV ingest: skipping %s (type=%s) per skip_jira_issue_types",
                    jira_key, issue_type,
                )
                continue

            sprint = _find_sprint(row).strip()
            if not sprint:
                log_decision(
                    "csv_issue_skip",
                    jira_key=jira_key,
                    reason="no_sprint",
                )
                logger.info("CSV ingest: skipping %s — no sprint assigned", jira_key)
                continue

            title = f"[{jira_key}] {summary}"
            body, missing = _build_issue_body(row, cfg.jira_base_url)
            conformant = len(missing) == 0

            for repo in repos:
                try:
                    if gh_issue_exists(repo, jira_key):
                        log_decision("csv_issue_skip", repo=repo, jira_key=jira_key, reason="already_exists")
                        continue

                    assignee = cfg.github_user if (conformant or not cfg.require_jira_conformance) else None
                    issue_number = gh_issue_create(repo, title, body, assignee=assignee)

                    if issue_number is None:
                        logger.warning("CSV ingest: failed to create issue for %s in %s", jira_key, repo)
                        continue

                    log_decision("csv_issue_created", repo=repo, jira_key=jira_key, issue=issue_number, conformant=conformant)
                    label_jira_issue(repo, issue_number, title)
                    logger.info("CSV ingest: created issue #%d for %s in %s%s",
                                issue_number, jira_key, repo,
                                " (non-conformant, not assigned)" if not conformant else "")

                    if not conformant:
                        needs_label = f"{cfg.github_user}:needs-jira-info"
                        try:
                            gh_ensure_label_exists(repo, needs_label, color="e11d48")
                            gh_issue_add_label(repo, issue_number, needs_label)
                        except Exception as e:
                            logger.warning("CSV ingest: could not add needs-jira-info label: %s", e)

                except Exception as e:
                    logger.error("CSV ingest: error processing %s in %s: %s", jira_key, repo, e)


def worktree_has_commits_since(worktree: Path, base_branch: str) -> bool:
    """Return True if the worktree has commits not on base_branch."""
    result = subprocess.run(
        ["git", "log", "--oneline", f"origin/{base_branch}..HEAD"],
        capture_output=True, text=True, cwd=str(worktree),
    )
    if result.returncode != 0:
        # Try without origin/ prefix (detached worktrees)
        result = subprocess.run(
            ["git", "log", "--oneline", f"{base_branch}..HEAD"],
            capture_output=True, text=True, cwd=str(worktree),
        )
    return bool(result.stdout.strip())


def rollback_issue(cfg: Config, state: dict, key: str, reason: str) -> None:
    """Remove labels, delete worktree, clear state entry."""
    entry = state.get(key, {})
    repo = entry.get("repo", "")
    issue_number = entry.get("issue_number")

    logger.warning("Rolling back %s: %s", key, reason)
    log_decision("rollback", repo=entry.get("repo", ""), issue=entry.get("issue_number"), reason=reason)

    # Remove all hunter labels from GitHub
    if repo and issue_number:
        for suffix in (":in-progress", ":proposal-open", ":implementing", ":awaiting-verification"):
            label = f"{cfg.github_user}{suffix}"
            try:
                gh_issue_remove_label(repo, issue_number, label)
            except Exception:
                pass

    # Delete worktrees
    for wt_field in ("proposal_worktree", "impl_worktree"):
        wt = entry.get(wt_field)
        if wt and Path(wt).exists():
            try:
                shutil.rmtree(wt)
                logger.info("Deleted worktree %s", wt)
            except Exception as e:
                logger.warning("Could not delete worktree %s: %s", wt, e)

    # Remove from state so it will be picked up fresh
    state.pop(key, None)
    save_hunter_state(state)
    logger.info("Rolled back %s — will retry on next poll", key)


def resume_in_flight_issues(cfg: Config, state: dict) -> None:
    """
    Called at the start of each poll cycle.
    Inspects non-terminal issues and either resumes them or rolls them back.
    Also cleans up orphaned labels (in-progress label but no state entry).
    """
    for key, entry in list(state.items()):
        status = entry.get("status", "")
        if status in TERMINAL_STATES or status == "":
            continue

        resume_attempts = entry.get("resume_attempts", 0)
        if resume_attempts >= cfg.max_resume_retries:
            log_decision("rollback", repo=entry.get("repo", ""), issue=entry.get("issue_number"), reason="max_resume_retries_exceeded")
            rollback_issue(cfg, state, key, f"exceeded max_resume_retries ({cfg.max_resume_retries})")
            continue

        repo = entry.get("repo", "")
        issue_number = entry.get("issue_number")

        if status == "in_progress":
            # Was mid-proposal-creation — check if worktree has commits
            wt = entry.get("proposal_worktree")
            if wt and Path(wt).exists():
                base = entry.get("base_branch", "main")
                if worktree_has_commits_since(Path(wt), base):
                    logger.info("Resuming %s from in_progress — worktree has commits, checking for existing PR", key)
                    # Check if a PR already exists with our marker
                    marker = f"hunter:issue-{issue_number}"
                    try:
                        prs = gh_list_prs_with_marker(repo, marker)
                        if prs:
                            pr_number = prs[0]["number"]
                            logger.info("Found existing proposal PR #%d for %s — advancing to proposal_open", pr_number, key)
                            update_issue_state(state, key,
                                               status="proposal_open",
                                               proposal_pr=pr_number,
                                               resume_attempts=resume_attempts + 1)
                        else:
                            logger.info("No PR found for %s — incrementing resume_attempts", key)
                            update_issue_state(state, key, resume_attempts=resume_attempts + 1)
                    except Exception as e:
                        logger.warning("Could not check PRs for %s: %s", key, e)
                else:
                    rollback_issue(cfg, state, key, "worktree exists but has no commits")
            else:
                rollback_issue(cfg, state, key, "in_progress with no worktree")

        elif status == "proposal_open":
            # Check proposal PR still exists
            proposal_pr = entry.get("proposal_pr")
            if not proposal_pr:
                rollback_issue(cfg, state, key, "proposal_open with no proposal_pr")
            # Otherwise fine — poll loop will check merge status

        elif status in ("implementing", "self_reviewing"):
            wt = entry.get("impl_worktree")
            if not entry.get("impl_pr"):
                # Implementation was started but PR not created yet
                if wt and Path(wt).exists():
                    logger.info("Resuming %s — impl worktree exists, checking for existing impl PR", key)
                    marker = f"hunter:impl-{issue_number}"
                    try:
                        prs = gh_list_prs_with_marker(repo, marker)
                        if prs:
                            pr_number = prs[0]["number"]
                            logger.info("Found existing impl PR #%d for %s", pr_number, key)
                            update_issue_state(state, key,
                                               impl_pr=pr_number,
                                               resume_attempts=resume_attempts + 1)
                        else:
                            update_issue_state(state, key, resume_attempts=resume_attempts + 1)
                    except Exception as e:
                        logger.warning("Could not check impl PRs for %s: %s", key, e)
                else:
                    # Roll back to proposal_open so we re-run impl from scratch
                    logger.warning("Resetting %s to proposal_open — impl worktree missing", key)
                    update_issue_state(state, key,
                                       status="proposal_open",
                                       impl_pr=None,
                                       impl_worktree=None,
                                       resume_attempts=resume_attempts + 1)

        elif status == "ready_for_review":
            impl_pr = entry.get("impl_pr")
            if not impl_pr:
                rollback_issue(cfg, state, key, "ready_for_review with no impl_pr")


def scan_orphaned_labels(cfg: Config, state: dict, repos: list[str]) -> None:
    """
    Remove hunter-owned labels from issues that have no matching hunter state entry
    or are in a terminal state. Runs at startup and periodically.
    """
    labels_to_check = [
        f"{cfg.github_user}:in-progress",
        f"{cfg.github_user}:proposal-open",
        f"{cfg.github_user}:implementing",
    ]
    for repo in repos:
        for label in labels_to_check:
            try:
                result = gh_run([
                    "issue", "list",
                    "--repo", repo,
                    "--state", "open",
                    "--label", label,
                    "--json", "number",
                    "--limit", "50",
                ], check=False)
                if result.returncode != 0:
                    continue
                issues = json.loads(result.stdout)
                for issue in issues:
                    issue_number = issue["number"]
                    key = f"{repo}!{issue_number}"
                    entry = state.get(key, {})
                    status = entry.get("status", "")
                    if not entry or status == "failed":
                        logger.warning("Removing orphaned label %s from issue %s",
                                       label, key)
                        try:
                            gh_issue_remove_label(repo, issue_number, label)
                        except Exception as e:
                            logger.warning("Could not remove orphaned label from %s: %s",
                                           key, e)
            except Exception as e:
                logger.warning("Could not scan orphaned labels for %s/%s: %s",
                               repo, label, e)


def _issue_has_hunter_labels(issue: dict) -> bool:
    """Return True if issue already has any hunter-style labels."""
    label_names = [lbl["name"] for lbl in issue.get("labels", [])]
    return any(
        any(lbl.endswith(suffix) for suffix in HUNTER_LABEL_PREFIXES)
        for lbl in label_names
    )


# ---------------------------------------------------------------------------
# Auto-label PR helpers (SPEC 1)
# ---------------------------------------------------------------------------

_PROPOSAL_TITLE_RE = re.compile(
    r"^(proposal|propose|sdd|design|rfc|spec)[:\s]", re.IGNORECASE)
_IMPL_TITLE_RE = re.compile(
    r"^(impl|implement|feat|fix|chore)[:\s]", re.IGNORECASE)


def _is_obviously_proposal(pr: dict) -> bool:
    title = pr.get("title") or ""
    branch = pr.get("headRefName") or ""
    files = pr.get("files") or []
    file_paths = [f.get("path", "") for f in files]
    if _PROPOSAL_TITLE_RE.match(title):
        return True
    if "/proposal" in branch or "-proposal" in branch:
        return True
    # adds files under openspec/changes/
    if any("openspec/changes/" in p for p in file_paths):
        return True
    return False


def _is_obviously_implementation(pr: dict) -> bool:
    title = pr.get("title") or ""
    branch = pr.get("headRefName") or ""
    files = pr.get("files") or []
    file_paths = [f.get("path", "") for f in files]
    # impl branch with matching title
    if ("/impl" in branch or "-impl" in branch) and _IMPL_TITLE_RE.match(title):
        return True
    # archives a proposal (explicit signal of implementation completion)
    if any("openspec/archive/" in p for p in file_paths):
        return True
    return False


def auto_label_prs(cfg: Config, repos: list[str]) -> None:
    if not cfg.auto_label_prs:
        return
    for repo in repos:
        try:
            result = gh_run([
                "pr", "list", "--repo", repo, "--state", "open",
                "--author", cfg.github_user,
                "--json", "number,title,headRefName,labels,files",
                "--limit", "100",
            ], check=False)
            if result.returncode != 0:
                continue
            prs = json.loads(result.stdout)
        except Exception as e:
            logger.warning("auto_label_prs: failed to list PRs for %s: %s", repo, e)
            continue

        for pr in prs:
            label_names = {lbl.get("name", "") for lbl in pr.get("labels") or []}
            if "sdd-proposal" in label_names or "sdd-implementation" in label_names:
                continue

            if _is_obviously_proposal(pr):
                try:
                    gh_ensure_label_exists(repo, "sdd-proposal")
                    gh_run(["pr", "edit", str(pr["number"]), "--repo", repo,
                            "--add-label", "sdd-proposal"])
                    logger.info("Auto-labeled PR #%d as sdd-proposal in %s",
                                pr["number"], repo)
                except Exception as e:
                    logger.warning("auto_label_prs: could not label PR #%d: %s",
                                   pr["number"], e)
            elif _is_obviously_implementation(pr):
                try:
                    gh_ensure_label_exists(repo, "sdd-implementation", color="e4e669")
                    gh_run(["pr", "edit", str(pr["number"]), "--repo", repo,
                            "--add-label", "sdd-implementation"])
                    logger.info("Auto-labeled PR #%d as sdd-implementation in %s",
                                pr["number"], repo)
                except Exception as e:
                    logger.warning("auto_label_prs: could not label PR #%d: %s",
                                   pr["number"], e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """hunter: daemon that picks up issues, writes proposals, implements, and self-reviews."""
    pass


@cli.command(name="status-server")
@click.option("--port", type=int, default=None, help="Port to run status server on (default from config).")
def status_server_cmd(port: int):
    """Start the status server (without hunter daemon)."""
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


@cli.command()
@click.option("--once", is_flag=True, help="Run a single poll iteration then exit.")
def start(once: bool):
    """Run the hunter daemon."""
    setup_logging()
    cfg = load_config()
    cfg.worktree_base.mkdir(parents=True, exist_ok=True)

    if not once:
        acquire_pid_file()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    _stop.clear()
    _current_issue_key[:] = []

    logger.info("hunter started (once=%s)", once)

    if not once and cfg.status_server_enabled:
        start_status_server(cfg.status_port)

    hunter_repos = list(dict.fromkeys(cfg.repos + cfg.hunter_only_repos))

    poll_cycle_count = 0
    # Scan for orphaned labels from crashed previous runs
    state = load_hunter_state()
    scan_orphaned_labels(cfg, state, hunter_repos)
    # One-shot sweep: label existing issues with Jira keys
    _sweep_jira_labels(cfg, hunter_repos)

    try:
        while not _stop.is_set():
            poll_cycle_count += 1
            state = load_hunter_state()

            # Periodic orphaned-label scan
            if cfg.orphan_scan_interval > 0 and poll_cycle_count % cfg.orphan_scan_interval == 0:
                scan_orphaned_labels(cfg, state, hunter_repos)
                state = load_hunter_state()

            # Ingest new issues from Jira CSV exports
            ingest_jira_csv(cfg, hunter_repos)

            # Auto-label unlabelled proposal/implementation PRs
            auto_label_prs(cfg, hunter_repos)

            # Resume or rollback any in-flight issues from previous runs
            resume_in_flight_issues(cfg, state)
            state = load_hunter_state()

            for repo in hunter_repos:
                if _stop.is_set():
                    break

                new_issues_this_cycle = 0

                # --- Poll assigned issues ---
                try:
                    issues = gh_list_assigned_issues(repo)
                except Exception as e:
                    logger.error("Failed to list issues for %s: %s", repo, e)
                    issues = []

                for issue in issues:
                    if _stop.is_set():
                        break
                    issue_number = issue["number"]
                    key = f"{repo}!{issue_number}"
                    entry = state.get(key, {})
                    status = entry.get("status", "")

                    # Skip terminal states
                    if status in TERMINAL_STATES:
                        continue

                    # Skip already-claimed issues (they have labels or are in-flight)
                    if status == "":
                        # New issue: only process if no competing labels
                        if _issue_has_hunter_labels(issue):
                            log_decision("issue_skip", repo=repo, issue=issue_number, reason="already_claimed")
                            continue
                        if new_issues_this_cycle >= cfg.max_new_issues_per_cycle:
                            log_decision("issue_skip", repo=repo, issue=issue_number, reason="max_new_issues_reached")
                            continue
                        _current_issue_key[:] = [key]
                        process_issue(cfg, state, repo, issue)
                        _current_issue_key.clear()
                        new_issues_this_cycle += 1
                        # Reload state after processing
                        state = load_hunter_state()

                # --- Advance in-flight issues ---
                state = load_hunter_state()
                for key, entry in list(state.items()):
                    if _stop.is_set():
                        break
                    if entry.get("repo") != repo:
                        continue
                    status = entry.get("status", "")
                    if status in TERMINAL_STATES:
                        continue

                    _current_issue_key[:] = [key]
                    try:
                        if status == "proposal_open":
                            check_proposal_merged(cfg, state, repo, key, entry)
                        elif status in ("implementing", "self_reviewing"):
                            # After proposal merges, check if impl PR is ready
                            if entry.get("impl_pr"):
                                check_impl_ready_for_review(cfg, state, repo, key, entry)
                        elif status == "ready_for_review":
                            check_impl_merged(cfg, state, repo, key, entry)
                    except Exception as e:
                        logger.error("Error advancing %s (status=%s): %s", key, status, e, exc_info=True)
                    finally:
                        _current_issue_key.clear()

                    # Reload after each advancement
                    state = load_hunter_state()

            logger.info("poll complete — next in %ds", cfg.poll_interval)
            if once or _stop.wait(cfg.poll_interval):
                break

        logger.info("hunter shutting down cleanly.")
    finally:
        stop_status_server()
        if not once:
            release_pid_file()


@cli.command(name="list")
def list_cmd():
    """Show all tracked issues and their status."""
    state = load_hunter_state()
    if not state:
        click.echo("No tracked issues.")
        return
    click.echo(json.dumps(state, indent=2))


@cli.command()
@click.argument("issue")
def show(issue: str):
    """Print the proposal or impl draft for an issue."""
    state = load_hunter_state()
    # Accept "repo!number" or plain number
    m = re.match(r"^([^!]+)!(\d+)$", issue)
    if m:
        key = issue
    else:
        try:
            num = int(issue)
        except ValueError:
            raise click.BadParameter(f"Cannot parse issue '{issue}'. Use 'owner/repo!123' or '123'.")
        # Find matching key
        matches = [k for k in state if k.endswith(f"!{num}")]
        if not matches:
            raise click.ClickException(f"Issue {issue} not found in state.")
        if len(matches) > 1:
            raise click.ClickException(f"Ambiguous issue number {num}: {', '.join(matches)}")
        key = matches[0]

    entry = state.get(key)
    if not entry:
        raise click.ClickException(f"Issue {key} not found in state.")

    click.echo(f"Key: {key}")
    click.echo(f"Status: {entry.get('status', 'unknown')}")
    if entry.get("proposal_pr"):
        click.echo(f"Proposal PR: #{entry['proposal_pr']}")
    if entry.get("impl_pr"):
        click.echo(f"Impl PR: #{entry['impl_pr']}")
    click.echo(json.dumps(entry, indent=2))


@cli.command()
def status():
    """Show counts by state."""
    state = load_hunter_state()
    counts: dict[str, int] = {}
    for entry in state.values():
        s = entry.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    if not counts:
        click.echo("No tracked issues.")
        return
    for s, c in sorted(counts.items()):
        click.echo(f"{s}: {c}")


if __name__ == "__main__":
    cli()
