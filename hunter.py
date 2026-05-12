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
from datetime import datetime, timezone
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
_run_claude = _predd._run_claude
_PWSH = _predd._PWSH
_DEVIN_STRIP_ENV = _predd._DEVIN_STRIP_ENV
repo_slug = _predd.repo_slug
find_local_repo = _predd.find_local_repo
setup_new_branch_worktree = _predd.setup_new_branch_worktree

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_DIR = Path("~/.config/predd").expanduser()
HUNTER_STATE_FILE = CONFIG_DIR / "hunter-state.json"
HUNTER_PID_FILE = CONFIG_DIR / "hunter-pid"
HUNTER_LOG_FILE = CONFIG_DIR / "hunter-log.txt"

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


def gh_run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        check=check,
    )


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
    issue_marker = f"#{issue_number}"
    for pr in prs:
        body = pr.get("body") or ""
        pr_title = pr.get("title") or ""
        if issue_marker in body or issue_marker in pr_title:
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
    return f"{cfg.branch_prefix}/{issue_number}-proposal-{issue_slug(title)}"


def impl_branch(cfg: Config, issue_number: int, title: str) -> str:
    return f"{cfg.branch_prefix}/{issue_number}-impl-{issue_slug(title)}"


# ---------------------------------------------------------------------------
# Skill runner
# ---------------------------------------------------------------------------


def _run_devin_skill(cfg: Config, prompt: str, skill_path: Path, worktree: Path) -> str:
    """Run a skill via Devin, placing skill file in .devin/skills/."""
    skill_dir = worktree / ".devin" / "skills"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_name = skill_path.stem.lower()
    (skill_dir / f"{skill_name}.md").write_text(skill_path.read_text())
    env = {k: v for k, v in os.environ.items() if k not in _DEVIN_STRIP_ENV}
    return _run_proc(
        ["setsid", "devin", "-p", "--permission-mode", "auto",
         "--model", cfg.model, "--", prompt],
        worktree,
        env=env,
    )


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

    if not try_claim_issue(cfg, repo, issue_number):
        logger.info("Could not claim issue %s — skipping", key)
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
        logger.info("Proposal PR #%d created for issue %s", pr_number, key)

    except Exception as e:
        logger.error("Failed processing issue %s: %s", key, e, exc_info=True)
        update_issue_state(state, key, status="failed")


def check_proposal_merged(cfg: Config, state: dict, repo: str, key: str, entry: dict) -> None:
    """If a merged sdd-proposal PR exists for this issue, kick off implementation."""
    issue_number = entry["issue_number"]
    title = entry["title"]

    merged_pr = gh_find_merged_proposal(repo, issue_number, title)
    if not merged_pr:
        return

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
        gh_ensure_label_exists(repo, implementing_label)
        gh_issue_add_label(repo, issue_number, implementing_label)

        update_issue_state(state, key,
                           status="implementing",
                           impl_pr=pr_number,
                           impl_branch=branch,
                           impl_worktree=str(worktree))
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
        _run_claude(cfg, fix_prompt, worktree) if cfg.backend == "claude" else \
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
    impl_pr = entry.get("impl_pr")
    if not impl_pr:
        return

    try:
        is_draft = gh_pr_is_draft(repo, impl_pr)
    except Exception as e:
        logger.warning("Could not check draft status for PR %d: %s", impl_pr, e)
        return

    if is_draft and not cfg.auto_review_draft:
        return

    worktree_str = entry.get("impl_worktree")
    worktree = Path(worktree_str) if worktree_str else cfg.worktree_base / "hunter-review"
    if not Path(worktree).exists():
        Path(worktree).mkdir(parents=True, exist_ok=True)

    self_review_loop(cfg, state, repo, key, entry, worktree)


def check_impl_merged(
    cfg: Config, state: dict, repo: str, key: str, entry: dict
) -> None:
    """If impl PR merged, hand back to reporter."""
    impl_pr = entry.get("impl_pr")
    if not impl_pr:
        return

    try:
        if not gh_pr_is_merged(repo, impl_pr):
            return
    except Exception as e:
        logger.warning("Could not check impl PR %d for %s: %s", impl_pr, key, e)
        return

    issue_number = entry["issue_number"]
    issue_author = entry.get("issue_author", "")
    logger.info("Impl PR #%d merged for %s — handing back to reporter", impl_pr, key)

    try:
        comment = (
            f"Implementation merged in #{impl_pr}. "
            "Please verify and close when confirmed."
        )
        gh_issue_reopen_and_reassign(repo, issue_number, issue_author, comment)

        awaiting_label = f"{cfg.github_user}:awaiting-verification"
        gh_ensure_label_exists(repo, awaiting_label)
        gh_issue_add_label(repo, issue_number, awaiting_label)

        update_issue_state(state, key, status="awaiting_verification")
        logger.info("Issue %s handed back to %s", key, issue_author)

    except Exception as e:
        logger.error("Failed post-merge handoff for %s: %s", key, e, exc_info=True)
        update_issue_state(state, key, status="failed")


# ---------------------------------------------------------------------------
# Resume and rollback
# ---------------------------------------------------------------------------

TERMINAL_STATES = {"merged", "awaiting_verification", "failed"}
HUNTER_LABEL_PREFIXES = (":in-progress", ":proposal-open", ":implementing", ":awaiting-verification")


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
    Remove in-progress labels from issues that have no hunter state entry.
    Runs once on startup.
    """
    label = f"{cfg.github_user}:in-progress"
    for repo in repos:
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
                if key not in state:
                    logger.warning("Removing orphaned label %s from issue %s", label, key)
                    try:
                        gh_issue_remove_label(repo, issue_number, label)
                    except Exception as e:
                        logger.warning("Could not remove orphaned label from %s: %s", key, e)
        except Exception as e:
            logger.warning("Could not scan orphaned labels for %s: %s", repo, e)


def _issue_has_hunter_labels(issue: dict) -> bool:
    """Return True if issue already has any hunter-style labels."""
    label_names = [lbl["name"] for lbl in issue.get("labels", [])]
    return any(
        any(lbl.endswith(suffix) for suffix in HUNTER_LABEL_PREFIXES)
        for lbl in label_names
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """hunter: daemon that picks up issues, writes proposals, implements, and self-reviews."""
    pass


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

    hunter_repos = list(dict.fromkeys(cfg.repos + cfg.hunter_only_repos))

    # Scan for orphaned labels from crashed previous runs
    state = load_hunter_state()
    scan_orphaned_labels(cfg, state, hunter_repos)

    try:
        while not _stop.is_set():
            state = load_hunter_state()

            # Resume or rollback any in-flight issues from previous runs
            resume_in_flight_issues(cfg, state)
            state = load_hunter_state()

            for repo in hunter_repos:
                if _stop.is_set():
                    break

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
                            continue
                        _current_issue_key[:] = [key]
                        process_issue(cfg, state, repo, issue)
                        _current_issue_key.clear()
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

            if once or _stop.wait(cfg.poll_interval):
                break

        logger.info("hunter shutting down cleanly.")
    finally:
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
