#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "click",
#   "anthropic[bedrock]>=0.42.0",
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
import tempfile
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
JiraClient = _predd.JiraClient
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

# ---------------------------------------------------------------------------
# Jira API circuit-breaker state
# ---------------------------------------------------------------------------

_jira_consecutive_failures: int = 0
_jira_backoff_until: float = 0.0
_JIRA_BACKOFF_BASE: int = 60
_JIRA_BACKOFF_MAX: int = 3600
_JIRA_FAILURE_THRESHOLD: int = 3

# Failure comment templates for hunter
_HUNTER_NO_COMMITS_COMMENT = """\
⚠️ Hunter ran the `{skill}` skill for this issue but the AI produced no commits.

**What this means:** The AI couldn't determine what to build from the current issue description.

**To unblock this issue, add one or more of the following to the description:**

- **File/directory references** — which files or modules should be changed? (e.g. `skills/my_skill/`, `blueprint_assist/handlers.py`)
- **Concrete acceptance criteria** — what should work after this is implemented?
- **Node type / API references** — if this involves specific types or APIs, name them explicitly (e.g. `dell.nodes.Compute`, not generic names)
- **Pointer to related code** — link a file, class, or function that is the starting point
- **What NOT to do** — constraints or anti-patterns the AI should avoid

Once the description is updated, hunter will retry automatically on the next cycle.
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

class SpeckitSkipError(Exception):
    """Raised by run_speckit_plan when required BPA-Specs artifacts are missing.

    Distinct from RuntimeError so process_issue can suppress the generic crash
    comment (run_speckit_plan already posted the user-facing comment and label).
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


def parse_jira_frontmatter(body: str) -> dict:
    """Extract key/value pairs from <!-- jira-metadata ... --> block in a GitHub issue body."""
    import re as _re
    m = _re.search(r"<!-- jira-metadata\n(.*?)\n-->", body or "", _re.DOTALL)
    if not m:
        return {}
    result = {}
    for line in m.group(1).splitlines():
        if ": " in line:
            k, _, v = line.partition(": ")
            result[k.strip()] = v.strip()
    return result


def issue_identifier(issue_number: int, title: str) -> str:
    """Return Jira key from title if present, else GitHub issue number as string."""
    return extract_jira_key(title) or str(issue_number)


def _pr_title(pr_type: str, issue_title: str) -> str:
    """Format a PR title.

    pr_type: "Proposal" or "Impl"
    issue_title: raw GitHub issue title, may contain leading [JIRA-ID]

    Output: "[JIRA-ID] Proposal/Impl - clean title"  or  "Proposal/Impl - clean title"
    """
    jira_key = extract_jira_key(issue_title)
    clean = JIRA_KEY_RE.sub("", issue_title).strip(" -")
    if jira_key:
        return f"[{jira_key}] {pr_type} - {clean}"
    return f"{pr_type} - {clean}"


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


def _cleanup_ephemeral_states(state: dict) -> int:
    """Reset in_progress/specifying and implementing entries to failed. Returns count reset."""
    EPHEMERAL = {"in_progress", "specifying", "implementing"}
    changed = []
    for key, entry in state.items():
        if entry.get("status") in EPHEMERAL:
            entry["status"] = "failed"
            changed.append(key)
    if changed:
        save_hunter_state(state)
        for key in changed:
            logger.info("Shutdown cleanup: reset %s to 'failed' (was ephemeral)", key)
    return len(changed)


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


def _find_impl_pr(repo: str, issue_number: int) -> int | None:
    """Find any sdd-implementation PR (open or merged) for this issue. Returns PR number or None."""
    result = gh_run([
        "pr", "list",
        "--repo", repo,
        "--state", "all",
        "--label", "sdd-implementation",
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


def _find_open_impl_pr_by_jira_key(repo: str, jira_key: str, exclude_issue: int) -> int | None:
    """Return PR number if any open sdd-implementation PR for jira_key exists on a different issue."""
    result = gh_run([
        "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--label", "sdd-implementation",
        "--json", "number,title,headRefName,body",
        "--limit", "100",
    ], check=False)
    if result.returncode != 0:
        return None
    key_pattern = re.compile(rf"\b{re.escape(jira_key)}\b", re.IGNORECASE)
    issue_ref_pattern = re.compile(r"hunter:issue-(\d+)")
    for pr in json.loads(result.stdout):
        branch = pr.get("headRefName", "")
        pr_title = pr.get("title") or ""
        body = pr.get("body") or ""
        if not (key_pattern.search(branch) or key_pattern.search(pr_title)):
            continue
        m = issue_ref_pattern.search(body)
        referenced_issue = int(m.group(1)) if m else 0
        if referenced_issue != exclude_issue:
            return pr["number"]
    return None


def reconcile_assigned_issues(cfg: "Config", state: dict, repos: list[str]) -> None:
    """For any assigned GH issue with no state entry, infer state from GitHub and inject it."""
    for repo in repos:
        try:
            issues = gh_list_assigned_issues(repo)
        except Exception as e:
            logger.warning("reconcile: failed to list issues for %s: %s", repo, e)
            continue

        for issue in issues:
            issue_number = issue["number"]
            title = issue["title"]
            key = f"{repo}!{issue_number}"

            if key in state:
                continue  # already tracked

            # Only reconcile issues with the "jira" label
            label_names = [lbl["name"] for lbl in issue.get("labels", [])]
            if "jira" not in label_names:
                continue

            # Search for impl PR first (highest specificity)
            impl_pr = _find_impl_pr(repo, issue_number)
            if impl_pr is not None:
                try:
                    merged = gh_pr_is_merged(repo, impl_pr)
                except Exception:
                    merged = False
                injected = "submitted" if merged else "implementing"
                entry = dict(
                    status=injected,
                    repo=repo,
                    issue_number=issue_number,
                    title=title,
                    impl_pr=impl_pr if not merged else None,
                    resume_attempts=0,
                )
                state[key] = entry
                save_hunter_state(state)
                logger.info("Reconciled %s: injected status %r (found %s impl PR #%d)",
                            key, injected, "merged" if merged else "open", impl_pr)
                log_decision("reconciled", repo=repo, issue=issue_number,
                             injected_status=injected, pr=impl_pr)
                continue

            # Search for merged proposal PR
            proposal_pr = gh_find_merged_proposal(repo, issue_number, title)
            if proposal_pr is not None:
                entry = dict(
                    status="proposal_open",
                    repo=repo,
                    issue_number=issue_number,
                    title=title,
                    proposal_pr=proposal_pr,
                    resume_attempts=0,
                )
                state[key] = entry
                save_hunter_state(state)
                logger.info("Reconciled %s: injected status 'proposal_open' "
                            "(found merged proposal PR #%d)", key, proposal_pr)
                log_decision("reconciled", repo=repo, issue=issue_number,
                             injected_status="proposal_open", pr=proposal_pr)


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
    """Lowercase title, replace non-alphanumeric with hyphens, truncate.

    Strips a leading [JIRA-KEY] prefix (e.g. "[DAP09A-1] ") before slugifying
    so the key does not appear twice when combined with issue_identifier().
    """
    # Strip leading [PROJECT-123] prefix before downcasing
    stripped = re.sub(r"^\[[A-Z][A-Z0-9]+-\d+\]\s*", "", title)
    slug = stripped.lower()
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
# Spec Kit (Phase I)
# ---------------------------------------------------------------------------


def _extract_epic_info(fields: dict) -> tuple[str, str]:
    """Return (epic_key, epic_name) from a Jira API issue fields dict.

    Tries customfield_10014 (Epic Link) first, then parent.key.
    Epic name is sourced from customfield_10014_detail (present on many Jira Server
    versions) or parent.fields.summary when the epic key comes from parent.
    """
    epic_key = fields.get("customfield_10014") or ""
    if not epic_key:
        parent = fields.get("parent") or {}
        epic_key = parent.get("key", "")
    if not epic_key:
        return "", ""

    # Try to get the epic name.
    # Path 1: customfield_10014_detail (Jira Server often returns this alongside
    #          customfield_10014 when you request the field).
    detail = fields.get("customfield_10014_detail") or {}
    if isinstance(detail, dict):
        epic_name = detail.get("summary") or detail.get("displayName") or detail.get("name") or ""
        if epic_name:
            return epic_key, epic_name

    # Path 2: parent.fields.summary (works when the epic is the direct parent,
    #          i.e. the epic key came from parent.key, not customfield_10014).
    parent = fields.get("parent") or {}
    if parent.get("key") == epic_key:
        epic_name = (parent.get("fields") or {}).get("summary", "")
        return epic_key, epic_name

    # Epic key known but name not available — slug resolution won't work;
    # speckit_epic_map is the only resolution path.
    logger.warning(
        "speckit: epic key %r found but name unavailable "
        "(customfield_10014_detail absent and parent.key mismatch); "
        "add %r to speckit_epic_map to enable slug resolution",
        epic_key, epic_key,
    )
    return epic_key, ""


def spec_branch(cfg: Config, issue_id: str, title_slug: str) -> str:
    """Branch name for speckit proposals: {branch_prefix}/{issue_id}-spec-{slug}."""
    return f"{cfg.branch_prefix}/{issue_id}-spec-{title_slug}"


def resolve_capability_folder(cfg: Config, epic_name: str, epic_key: str) -> "Path | None":
    """Return the capability folder path for the given epic, or None if not found."""
    if not cfg.capability_specs_path:
        return None
    # No epic at all — silently skip; no reason to log
    if not epic_name and not epic_key:
        return None
    # Slug match on epic name
    if epic_name:
        slug = re.sub(r"[^a-z0-9]+", "-", epic_name.lower()).strip("-")
        folder = cfg.capability_specs_path / slug
        if folder.exists():
            return folder
        logger.info("speckit: slug %r not found in %s", slug, cfg.capability_specs_path)
    # Map fallback via speckit_epic_map[epic_key]
    if epic_key and epic_key in cfg.speckit_epic_map:
        folder = cfg.capability_specs_path / cfg.speckit_epic_map[epic_key]
        if folder.exists():
            return folder
    log_decision("speckit_no_capability", epic_name=epic_name, epic_key=epic_key)
    logger.info("speckit: no capability folder for epic %r (%s) — legacy fallback",
                epic_name, epic_key)
    return None


def read_bpa_specs_bundle(capability_dir: Path, story_id: str) -> dict:
    """Read BPA-Specs artifacts for a story. Hard fails if required files missing."""
    story_dir = capability_dir / "stories" / story_id
    constitution = capability_dir / "constitution.md"
    capability_spec = capability_dir / "spec.md"
    story_spec = story_dir / "spec.md"
    clarifications = capability_dir / "clarifications.md"

    missing = []
    if not constitution.exists():
        missing.append("constitution.md")
    if not capability_spec.exists():
        missing.append("spec.md")
    if not story_spec.exists():
        missing.append(f"stories/{story_id}/spec.md")
    if missing:
        raise RuntimeError(
            f"Missing required speckit artifacts in {capability_dir}: {', '.join(missing)}"
        )

    result = {
        "constitution": constitution,
        "capability_spec": capability_spec,
        "story_spec": story_spec,
        "clarifications": clarifications if clarifications.exists() else None,
    }
    if not clarifications.exists():
        log_decision("speckit_no_clarifications",
                     capability=str(capability_dir.name), story_id=story_id)
        logger.info("speckit: no clarifications.md for %s — proceeding without",
                    capability_dir.name)
    return result


def pin_capability_sha(cfg: Config) -> str:
    """Return the HEAD SHA of the capability_specs_path repo.

    Raises RuntimeError (not CalledProcessError) if the path is not a git repo,
    so the error surfaces with a user-readable message rather than a raw traceback.
    """
    result = subprocess.run(
        ["git", "-C", str(cfg.capability_specs_path), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"capability_specs_path ({cfg.capability_specs_path}) is not a git repository "
            f"or has no commits: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def copy_spec_refs(bundle: dict, worktree: Path) -> Path:
    """Copy BPA-Specs bundle files into worktree/spec-refs/. Returns spec-refs path."""
    spec_refs = worktree / "spec-refs"
    spec_refs.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundle["constitution"], spec_refs / "constitution.md")
    shutil.copy2(bundle["capability_spec"], spec_refs / "capability-spec.md")
    shutil.copy2(bundle["story_spec"], spec_refs / "story-spec.md")
    if bundle.get("clarifications"):
        shutil.copy2(bundle["clarifications"], spec_refs / "clarifications.md")
    return spec_refs


def load_speckit_prompt(cfg: Config, template_name: str, **kwargs) -> str:
    """Read a speckit prompt template and render it with kwargs.

    Uses str.format() with named placeholders — literal braces in templates must
    be escaped as {{ and }} (standard Python format string rules).
    """
    template_path = cfg.speckit_prompt_dir / f"{template_name}.md"
    if not template_path.exists():
        raise FileNotFoundError(f"Speckit prompt template not found: {template_path}")
    text = template_path.read_text()
    try:
        return text.format(**kwargs)
    except KeyError as e:
        raise KeyError(
            f"Speckit template {template_path.name!r} references placeholder {e} "
            f"that was not supplied. Check the template for typos or unescaped braces."
        ) from e


def run_speckit_plan(cfg: Config, entry: dict, worktree: Path,
                     issue_number: int, title: str, issue_body: str) -> bool:
    """Run the speckit plan phase. Returns True if speckit ran, False to fall back to legacy."""
    jira_key = entry.get("jira_key", "")
    epic_name = entry.get("epic_name", "")
    epic_key = entry.get("epic_key", "")

    capability_dir = resolve_capability_folder(cfg, epic_name, epic_key)
    if capability_dir is None:
        return False

    story_id = jira_key or str(issue_number)
    repo = entry.get("repo", "")
    try:
        bundle = read_bpa_specs_bundle(capability_dir, story_id)
    except RuntimeError as e:
        missing_label = f"{cfg.github_user}:speckit-missing-spec"
        log_decision("speckit_missing_spec", issue=issue_number, story_id=story_id,
                     capability=str(capability_dir.name), reason=str(e))
        logger.warning("speckit: missing required artifacts for %s — %s", story_id, e)
        if repo:
            try:
                gh_ensure_label_exists(repo, missing_label, color="e11d48")
                gh_issue_add_label(repo, issue_number, missing_label)
                gh_issue_comment(repo, issue_number,
                    f"⚠️ Spec Kit cannot process this story: {e}\n\n"
                    "Add the missing artifacts to BPA-Specs, then remove the "
                    f"`{missing_label}` label to retry.")
            except Exception as label_err:
                logger.warning("speckit: could not apply missing-spec label: %s", label_err)
        # Raise SpeckitSkipError so process_issue suppresses the generic crash comment —
        # the user-facing message was already posted above.
        raise SpeckitSkipError(str(e)) from e
    sha = pin_capability_sha(cfg)

    spec_refs = copy_spec_refs(bundle, worktree)
    commit_skill_output(worktree, f"speckit: copy spec-refs for issue #{issue_number}")

    clarifications_path = (
        str(spec_refs / "clarifications.md")
        if bundle.get("clarifications") else "(not present)"
    )
    prompt = load_speckit_prompt(
        cfg, template_name="plan",
        issue_number=issue_number,
        issue_title=title,
        issue_body=issue_body or "(no description)",
        constitution_path=str(spec_refs / "constitution.md"),
        capability_spec_path=str(spec_refs / "capability-spec.md"),
        story_spec_path=str(spec_refs / "story-spec.md"),
        clarifications_path=clarifications_path,
        spec_refs_dir=str(spec_refs),
        capability_specs_sha=sha,
        capability=str(capability_dir.name),
        story_id=story_id,
    )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, prefix="speckit-plan-"
    ) as f:
        tmp_path = Path(f.name)
        f.write(prompt)

    context = build_issue_context(issue_number, title, issue_body, entry)
    try:
        run_skill(cfg, tmp_path, context, worktree)
        if not (worktree / "plan.md").exists():
            raise RuntimeError("speckit plan skill did not produce plan.md")
    finally:
        tmp_path.unlink(missing_ok=True)

    log_decision("plan_completed", issue=issue_number, story_id=story_id,
                 capability=str(capability_dir.name), sha=sha)
    return True


def run_speckit_implement(cfg: Config, entry: dict, worktree: Path,
                          issue_number: int, title: str, issue_body: str) -> None:
    """Run the speckit implement phase using spec-refs/ + plan.md from the merged proposal."""
    spec_refs = worktree / "spec-refs"
    plan_path = worktree / "plan.md"
    tasks_file = worktree / "tasks.md"
    tasks_path = str(tasks_file) if tasks_file.exists() else "(not present)"
    clarifications_file = spec_refs / "clarifications.md"
    clarifications_path = (
        str(clarifications_file) if clarifications_file.exists() else "(not present)"
    )

    prompt = load_speckit_prompt(
        cfg, template_name="implement",
        issue_number=issue_number,
        issue_title=title,
        issue_body=issue_body or "(no description)",
        spec_refs_dir=str(spec_refs),
        constitution_path=str(spec_refs / "constitution.md"),
        capability_spec_path=str(spec_refs / "capability-spec.md"),
        story_spec_path=str(spec_refs / "story-spec.md"),
        clarifications_path=clarifications_path,
        plan_path=str(plan_path),
        tasks_path=tasks_path,
    )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, prefix="speckit-impl-"
    ) as f:
        tmp_path = Path(f.name)
        f.write(prompt)

    context = build_issue_context(issue_number, title, issue_body, entry)
    try:
        run_skill(cfg, tmp_path, context, worktree)
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Core processing functions
# ---------------------------------------------------------------------------


def _clean_hunter_labels(repo: str, issue_number: int, cfg) -> None:
    labels_to_remove = [
        f"{cfg.github_user}:in-progress",
        f"{cfg.github_user}:proposal-open",
        f"{cfg.github_user}:spec-open",
        f"{cfg.github_user}:implementing",
        f"{cfg.github_user}:awaiting-verification",
        f"{cfg.github_user}:speckit-missing-spec",
        f"{cfg.github_user}:hunter-failed",
        "needs-jira-info",
        "needs-replan",
    ]
    for label in labels_to_remove:
        try:
            gh_issue_remove_label(repo, issue_number, label)
        except Exception:
            pass


def process_issue(cfg: Config, state: dict, repo: str, issue: dict) -> None:
    """Pick up a new issue: claim it, create proposal PR."""
    issue_number = issue["number"]
    title = issue["title"]
    key = f"{repo}!{issue_number}"

    logger.info("Picking up issue %s: %s", key, title)

    # Apply jira label if title contains a Jira key
    label_jira_issue(repo, issue_number, title)

    # Jira conformance check (if Jira API is configured)
    jira_key = extract_jira_key(title)
    jira_frontmatter = ""
    epic_key = ""
    epic_name = ""
    if jira_key:
        jira_frontmatter, missing_fields, issue_data = _fetch_jira_frontmatter(cfg, jira_key)
        epic_key, epic_name = _extract_epic_info(issue_data.get("fields") or {})
        if missing_fields and cfg.require_jira_conformance:
            needs_label = f"{cfg.github_user}:needs-jira-info"
            missing_lines = "\n".join(f"- No {f.lower()} assigned" if f != "Capability"
                                      else f"- No capability found in description (add `capability: <id> <name>`)"
                                      for f in missing_fields)
            comment = (
                f"⚠️ This issue is missing required Jira fields:\n{missing_lines}\n\n"
                "Hunter will not process this issue until it conforms."
            )
            try:
                gh_ensure_label_exists(repo, needs_label, color="e11d48")
                gh_issue_add_label(repo, issue_number, needs_label)
                gh_issue_comment(repo, issue_number, comment)
            except Exception as e:
                logger.warning("Could not apply needs-jira-info label/comment for %s#%d: %s",
                               repo, issue_number, e)
            log_decision("jira_conformance_failed", repo=repo, issue=issue_number,
                         jira_key=jira_key, missing=missing_fields)
            logger.info("Skipping %s#%d — non-conformant Jira issue (%s)",
                        repo, issue_number, ", ".join(missing_fields))
            update_issue_state(state, key,
                               status="skipped_conformance",
                               issue_number=issue_number,
                               repo=repo,
                               title=title,
                               missing_fields=missing_fields,
                               last_conformance_check=_now_iso(),
                               first_seen=_now_iso())
            return

    if not try_claim_issue(cfg, repo, issue_number):
        logger.info("Could not claim issue %s — skipping", key)
        log_decision("claim_failed", repo=repo, issue=issue_number, reason="try_claim_returned_false")
        return

    try:
        base_branch = gh_repo_default_branch(repo)
    except Exception as e:
        logger.error("Failed to get default branch for %s: %s", repo, e)
        return

    if cfg.speckit_enabled:
        branch = spec_branch(cfg, issue_identifier(issue_number, title), issue_slug(title))
    else:
        branch = proposal_branch(cfg, issue_number, title)
    worktree = cfg.worktree_base / f"{repo_slug(repo)}-{branch.replace('/', '-')}"

    issue_body = issue.get("body") or ""

    update_issue_state(state, key,
                       status="specifying" if cfg.speckit_enabled else "in_progress",
                       issue_number=issue_number,
                       repo=repo,
                       title=title,
                       issue_body=issue_body,
                       issue_author=issue.get("author", {}).get("login", ""),
                       base_branch=base_branch,
                       proposal_branch=branch,
                       proposal_worktree=str(worktree),
                       jira_frontmatter=jira_frontmatter,
                       jira_key=jira_key or "",
                       epic_key=epic_key,
                       epic_name=epic_name,
                       resume_attempts=0,
                       first_seen=_now_iso())

    worktree = None
    try:
        notify_sound(cfg.sound_new_pr)
        notify_toast("New issue picked up", f"{key} — {title}")

        worktree = setup_new_branch_worktree(cfg, repo, branch, base_branch)
        update_issue_state(state, key, proposal_worktree=str(worktree))
        logger.info("Proposal worktree at %s", worktree)

        context = build_issue_context(
            issue_number, title,
            state.get(key, {}).get("issue_body", ""),
            state.get(key, {}),
        )
        used_speckit = cfg.speckit_enabled and run_speckit_plan(
            cfg, state.get(key, {}), worktree, issue_number, title, issue_body,
        )
        if not used_speckit:
            run_skill(cfg, cfg.proposal_skill_path, context, worktree)
        update_issue_state(state, key, used_speckit=used_speckit)
        commit_skill_output(worktree, f"proposal: issue #{issue_number} — {title[:60]}")

        if not skill_has_commits(worktree):
            log_decision("skill_no_commits", repo=repo, issue=issue_number, skill="proposal")
            raise RuntimeError("Proposal skill produced no commits — not creating empty PR")

        frontmatter = state.get(key, {}).get("jira_frontmatter", "")
        pr_body = f"{frontmatter}Proposal for issue #{issue_number}\n\n<!-- hunter:issue-{issue_number} -->"
        gh_ensure_label_exists(repo, "sdd-proposal", color="0075ca")
        pr_number = gh_create_branch_and_pr(
            repo=repo,
            base=base_branch,
            branch=branch,
            title=_pr_title("Proposal", title),
            body=pr_body,
            draft=True,
            worktree=worktree,
            label="sdd-proposal",
        )

        in_progress_label = f"{cfg.github_user}:in-progress"
        open_label = f"{cfg.github_user}:{'spec-open' if used_speckit else 'proposal-open'}"
        gh_ensure_label_exists(repo, open_label)
        gh_issue_add_label(repo, issue_number, open_label)
        gh_issue_remove_label(repo, issue_number, in_progress_label)

        update_issue_state(state, key,
                           status="spec_open" if used_speckit else "proposal_open",
                           proposal_pr=pr_number,
                           proposal_branch=branch,
                           proposal_worktree=str(worktree))
        log_decision("issue_pickup", repo=repo, issue=issue_number, title=title)
        log_decision("proposal_created", repo=repo, issue=issue_number, pr=pr_number)
        logger.info("Proposal PR #%d created for issue %s", pr_number, key)

    except SpeckitSkipError as e:
        # Comment already posted by run_speckit_plan — don't post a second one.
        logger.warning("Skipping issue %s — speckit artifacts missing: %s", key, e)
        log_decision("speckit_skip", repo=repo, issue=issue_number, reason=str(e))
        update_issue_state(state, key, status="failed")
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
                        error=str(e),
                        worktree_path=str(worktree) if worktree is not None else "<not created>",
                    )
                gh_issue_comment(repo, issue_number, comment)
                failure_label = f"{cfg.github_user}:hunter-failed"
                gh_ensure_label_exists(repo, failure_label, color="e11d48")
                gh_issue_add_label(repo, issue_number, failure_label)
                log_decision("issue_failure_commented", repo=repo, issue=issue_number, reason=str(e)[:100])
            except Exception as comment_err:
                logger.error("Failed to post failure comment for %s: %s", key, comment_err)
        update_issue_state(state, key, status="failed")
        _clean_hunter_labels(repo, issue_number, cfg)
    except Exception as e:
        logger.error("Failed processing issue %s: %s", key, e, exc_info=True)
        if cfg.comment_on_failures:
            try:
                comment = _HUNTER_CRASH_COMMENT.format(
                    repo=repo, issue_number=issue_number, skill="proposal",
                    error=str(e),
                    worktree_path=str(worktree) if worktree is not None else "<not created>",
                )
                gh_issue_comment(repo, issue_number, comment)
                failure_label = f"{cfg.github_user}:hunter-failed"
                gh_ensure_label_exists(repo, failure_label, color="e11d48")
                gh_issue_add_label(repo, issue_number, failure_label)
                log_decision("issue_failure_commented", repo=repo, issue=issue_number, reason="crash", error=str(e)[:100])
            except Exception as comment_err:
                logger.error("Failed to post failure comment for %s: %s", key, comment_err)
        update_issue_state(state, key, status="failed")
        _clean_hunter_labels(repo, issue_number, cfg)


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

    # Speckit Phase II re-plan loop: if predd flagged the plan as inconsistent,
    # close the proposal PR and reset the issue for re-planning.
    if entry.get("used_speckit"):
        try:
            issue_detail = gh_issue_view(repo, issue_number)
            issue_labels = {lb["name"] for lb in issue_detail.get("labels", [])}
        except Exception:
            issue_labels = set()

        if "needs-replan" in issue_labels:
            loops = entry.get("analyze_fix_loops", 0)
            if loops >= cfg.max_analyze_fix_loops:
                logger.warning("%s: analyze fix loops exhausted (%d/%d) — marking failed",
                               key, loops, cfg.max_analyze_fix_loops)
                log_decision("analyze_loops_exhausted", repo=repo, issue=issue_number,
                             loops=loops)
                try:
                    gh_issue_comment(repo, issue_number,
                        f"Hunter: speckit analyze fix loop exhausted after {loops} attempt(s). "
                        f"Manual review required. Set `analyze_fix_loops` to 0 in state to retry.")
                except Exception:
                    pass
                update_issue_state(state, key, status="failed")
                return

            proposal_pr = entry.get("proposal_pr")
            logger.info("%s: needs-replan detected (loop %d/%d) — closing proposal PR and resetting",
                        key, loops + 1, cfg.max_analyze_fix_loops)
            if proposal_pr:
                try:
                    gh_run(["pr", "close", str(proposal_pr), "--repo", repo], check=False)
                except Exception:
                    pass
            try:
                gh_issue_remove_label(repo, issue_number, "needs-replan")
            except Exception:
                pass
            log_decision("replan_reset", repo=repo, issue=issue_number,
                         loop=loops + 1, pr=proposal_pr)
            update_issue_state(state, key,
                               status="new",
                               proposal_pr=None,
                               analyze_fix_loops=loops + 1)
            return

    # Collect feedback on the proposal PR while waiting for merge
    proposal_pr = entry.get("proposal_pr")
    if proposal_pr:
        collect_pr_feedback(cfg, state, repo, key, proposal_pr, "proposal_feedback")
        state = load_hunter_state()  # reload after potential state update
        entry = state.get(key, entry)

    merged_pr = gh_find_merged_proposal(repo, issue_number, title)
    if not merged_pr:
        return

    # If an impl PR already exists (open or merged), resume from it rather than
    # pushing a new branch and hitting a conflict.
    existing_impl = _find_impl_pr(repo, issue_number)
    if existing_impl:
        logger.info("Impl PR #%d already exists for %s — resuming", existing_impl, key)
        update_issue_state(state, key, status="implementing", impl_pr=existing_impl)
        return

    # Block duplicate impl PRs when another GitHub issue with the same Jira key
    # already has an open impl PR (e.g. Jira issue imported twice into GitHub).
    jira_key = extract_jira_key(title)
    if jira_key:
        duplicate_pr = _find_open_impl_pr_by_jira_key(repo, jira_key, exclude_issue=issue_number)
        if duplicate_pr:
            logger.warning(
                "Jira key %s already has open impl PR #%d — skipping impl for %s",
                jira_key, duplicate_pr, key,
            )
            log_decision("impl_skipped_jira_duplicate", repo=repo, issue=issue_number,
                         jira_key=jira_key, existing_pr=duplicate_pr)
            try:
                gh_issue_comment(repo, issue_number,
                    f"Skipping implementation: Jira key `{jira_key}` already has an open "
                    f"impl PR #{duplicate_pr}. If this is a separate issue, update the "
                    f"Jira key in the issue title to make them unique.")
            except Exception:
                pass
            update_issue_state(state, key, status="skipped_jira_duplicate")
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
        if entry.get("used_speckit"):
            run_speckit_implement(cfg, entry, worktree, issue_number, title,
                                  entry.get("issue_body", ""))
        else:
            run_skill(cfg, cfg.impl_skill_path, context, worktree)
        commit_skill_output(worktree, f"impl: issue #{issue_number} — {title[:60]}")

        if not skill_has_commits(worktree):
            log_decision("skill_no_commits", repo=repo, issue=issue_number, skill="impl")
            raise RuntimeError("Impl skill produced no commits — not creating empty PR")

        frontmatter = entry.get("jira_frontmatter", "")
        pr_body = f"{frontmatter}Implementation for issue #{issue_number}\n\n<!-- hunter:issue-{issue_number} -->\n<!-- hunter:impl-{issue_number} -->"
        gh_ensure_label_exists(repo, "sdd-implementation", color="e4e669")
        pr_number = gh_create_branch_and_pr(
            repo=repo,
            base=base_branch,
            branch=branch,
            title=_pr_title("Impl", title),
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

    except RuntimeError as e:
        logger.error("Failed starting implementation for %s: %s", key, e, exc_info=True)
        if cfg.comment_on_failures:
            try:
                if "no commits" in str(e).lower():
                    comment = _HUNTER_NO_COMMITS_COMMENT.format(
                        repo=repo, issue_number=issue_number, skill="impl"
                    )
                else:
                    comment = _HUNTER_CRASH_COMMENT.format(
                        repo=repo, issue_number=issue_number, skill="impl",
                        error=str(e), worktree_path=worktree
                    )
                gh_issue_comment(repo, issue_number, comment)
                failure_label = f"{cfg.github_user}:hunter-failed"
                gh_ensure_label_exists(repo, failure_label, color="e11d48")
                gh_issue_add_label(repo, issue_number, failure_label)
                log_decision("issue_failure_commented", repo=repo, issue=issue_number, reason=str(e)[:100])
            except Exception as comment_err:
                logger.error("Failed to post failure comment for %s: %s", key, comment_err)
        update_issue_state(state, key, status="failed")
    except Exception as e:
        logger.error("Failed starting implementation for %s: %s", key, e, exc_info=True)
        if cfg.comment_on_failures:
            try:
                comment = _HUNTER_CRASH_COMMENT.format(
                    repo=repo, issue_number=issue_number, skill="impl",
                    error=str(e), worktree_path=worktree
                )
                gh_issue_comment(repo, issue_number, comment)
                failure_label = f"{cfg.github_user}:hunter-failed"
                gh_ensure_label_exists(repo, failure_label, color="e11d48")
                gh_issue_add_label(repo, issue_number, failure_label)
                log_decision("issue_failure_commented", repo=repo, issue=issue_number, reason="crash", error=str(e)[:100])
            except Exception as comment_err:
                logger.error("Failed to post failure comment for %s: %s", key, comment_err)
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
        # Stage and commit fixes
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(worktree), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", f"fix: address review findings (loop {loops_done + 1})"],
            cwd=str(worktree), check=True, capture_output=True,
        )
    except Exception as e:
        logger.error("Fix loop failed for %s: %s", key, e, exc_info=True)
        update_issue_state(state, key, status="failed")
        return

    # Push fixes — separated so push failure can be recovered without losing commits
    try:
        subprocess.run(
            ["git", "push"],
            cwd=str(worktree), check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as push_err:
        logger.warning("Self-review push failed for %s: %s — resetting to implementing", key, push_err)
        log_decision("self_review_push_failed", repo=repo, issue=issue_number,
                     pr=impl_pr, error=str(push_err)[:200])
        update_issue_state(state, key,
                           status="implementing",
                           impl_push_failed=True,
                           review_loops_done=loops_done)
        return

    update_issue_state(state, key,
                       status="implementing",
                       impl_push_failed=False,
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
        _clean_hunter_labels(repo, issue_number, cfg)
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

TERMINAL_STATES = {"merged", "awaiting_verification", "submitted", "skipped_conformance"}
HUNTER_LABEL_PREFIXES = (":in-progress", ":proposal-open", ":implementing", ":awaiting-verification")


def _parse_capability(description: str) -> str | None:
    m = re.search(r"capability:\s*(\d+)\s+(.+)", description, re.IGNORECASE)
    if m:
        return f"{m.group(1)} — {m.group(2).strip()}"
    return None


# ---------------------------------------------------------------------------
# Jira API frontmatter
# ---------------------------------------------------------------------------

def _build_jira_frontmatter(issue_data: dict, jira_base_url: str) -> str:
    """Build a markdown table frontmatter block from a Jira issue dict.

    Returns a string ending with a separator line and a trailing newline, ready
    to be prepended to a PR body.
    """
    fields = issue_data.get("fields") or {}
    jira_key = issue_data.get("key", "")
    base_url = jira_base_url.rstrip("/")

    lines = ["| Field | Value |", "|-------|-------|"]

    # Jira row — always present when key is known
    if jira_key:
        lines.append(f"| Jira | [{jira_key}]({base_url}/browse/{jira_key}) |")

    # Type row — always present
    issue_type = (fields.get("issuetype") or {}).get("name", "")
    if issue_type:
        lines.append(f"| Type | {issue_type} |")

    # Epic row — try customfield_10014 (epic link key) then parent.key
    epic_key = fields.get("customfield_10014") or ""
    if not epic_key:
        parent = fields.get("parent") or {}
        epic_key = parent.get("key", "")
    if epic_key:
        # Try to get epic summary from customfield_10014_detail or parent.fields.summary
        epic_summary = ""
        parent = fields.get("parent") or {}
        if parent.get("key") == epic_key:
            epic_summary = (parent.get("fields") or {}).get("summary", "")
        if epic_summary:
            lines.append(f"| Epic | [{epic_key}]({base_url}/browse/{epic_key}) {epic_summary} |")
        else:
            lines.append(f"| Epic | [{epic_key}]({base_url}/browse/{epic_key}) |")

    # Sprint row — customfield_10020 is an array; take last entry's name
    sprints = fields.get("customfield_10020") or []
    sprint_name = ""
    if sprints and isinstance(sprints, list):
        last_sprint = sprints[-1]
        if isinstance(last_sprint, dict):
            sprint_name = last_sprint.get("name", "")
        else:
            sprint_name = str(last_sprint)
    if sprint_name:
        lines.append(f"| Sprint | {sprint_name} |")

    # Capability row — parsed from description
    description = fields.get("description") or ""
    capability = _parse_capability(description)
    if capability:
        lines.append(f"| Capability | {capability} |")

    return "\n".join(lines) + "\n\n---\n\n"


def _check_jira_conformance(issue_data: dict) -> list[str]:
    """Return list of missing required Jira field names.

    A non-empty list means the issue is non-conformant.
    """
    fields = issue_data.get("fields") or {}
    missing = []

    # Sprint
    sprints = fields.get("customfield_10020") or []
    if not sprints:
        missing.append("Sprint")

    # Epic
    epic_key = fields.get("customfield_10014") or ""
    if not epic_key:
        parent = fields.get("parent") or {}
        epic_key = parent.get("key", "")
    if not epic_key:
        missing.append("Epic")

    # Capability
    description = fields.get("description") or ""
    if not _parse_capability(description):
        missing.append("Capability")

    return missing


def _fetch_jira_frontmatter(cfg: "Config", jira_key: str) -> tuple[str, list[str], dict]:
    """Fetch Jira issue and build frontmatter. Returns (frontmatter, missing_fields, issue_data).

    Returns ("", [], {}) if Jira API is not configured or unavailable.
    """
    token = os.environ.get("JIRA_API_TOKEN")
    if not token or not cfg.jira_api_enabled:
        return "", [], {}

    try:
        client = JiraClient(cfg.jira_base_url, token)
        issue_data = client.get_issue(jira_key)
        missing = _check_jira_conformance(issue_data)
        frontmatter = _build_jira_frontmatter(issue_data, cfg.jira_base_url)
        return frontmatter, missing, issue_data
    except Exception as e:
        logger.warning("Could not fetch Jira data for %s: %s", jira_key, e)
        return "", [], {}


EPIC_COLUMNS = (
    "epic link",
    "epic name",
    "custom field (epic link)",
    "custom field (epic name)",
    "parent",
    "parent key",
)


def _find_epic(row: dict) -> str:
    """Find Epic value from the first non-empty column in EPIC_COLUMNS."""
    for col in EPIC_COLUMNS:
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
    sprint = row.get("sprint", "")
    description = row.get("description", "")
    capability = _parse_capability(description)
    parent_key = row.get("parent key", "")  # set for subtasks

    missing = []
    if not epic:
        missing.append("Epic not set")

    # Machine-parseable frontmatter block (HTML comment, invisible in rendered view)
    fm_lines = ["<!-- jira-metadata"]
    if jira_key:
        fm_lines.append(f"jira_key: {jira_key}")
    if issue_type:
        fm_lines.append(f"jira_type: {issue_type}")
    if epic:
        fm_lines.append(f"jira_epic: {epic}")
    if sprint:
        fm_lines.append(f"jira_sprint: {sprint}")
    if capability:
        fm_lines.append(f"jira_capability: {capability}")
    if parent_key:
        fm_lines.append(f"jira_parent: {parent_key}")
    if jira_key:
        fm_lines.append(f"jira_url: {jira_base_url}/browse/{jira_key}")
    fm_lines.append("-->")
    frontmatter = "\n".join(fm_lines)

    # Visible metadata table
    table_lines = ["| Field | Value |", "|-------|-------|"]
    if jira_key:
        table_lines.append(f"| Jira | [{jira_key}]({jira_base_url}/browse/{jira_key}) |")
    if issue_type:
        table_lines.append(f"| Type | {issue_type} |")
    if parent_key:
        table_lines.append(f"| Parent | [{parent_key}]({jira_base_url}/browse/{parent_key}) |")
    if epic:
        if re.match(r"^[A-Z]+-\d+$", epic):
            table_lines.append(f"| Epic | [{epic}]({jira_base_url}/browse/{epic}) |")
        else:
            table_lines.append(f"| Epic | {epic} |")
    if sprint:
        table_lines.append(f"| Sprint | {sprint} |")
    if capability:
        table_lines.append(f"| Capability | {capability} |")

    body = frontmatter + "\n\n" + "\n".join(table_lines)

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


def _find_gh_issue_for_jira_key(repo: str, jira_key: str) -> int | None:
    """Find open GitHub issue whose title contains [jira_key]. Returns issue number or None."""
    result = gh_run([
        "issue", "list", "--repo", repo, "--state", "open",
        "--json", "number,title", "--limit", "200",
    ], check=False)
    if result.returncode != 0:
        return None
    for issue in json.loads(result.stdout):
        if f"[{jira_key}]" in (issue.get("title") or ""):
            return issue["number"]
    return None


def gh_add_sub_issue(repo: str, parent_issue: int, sub_issue: int) -> None:
    """Link sub_issue as a sub-issue of parent_issue via GitHub API."""
    owner, name = repo.split("/", 1)
    gh_run([
        "api",
        f"repos/{owner}/{name}/issues/{parent_issue}/sub_issues",
        "--method", "POST",
        "--field", f"sub_issue_id={sub_issue}",
    ], check=False)


def _sprint_jql_clause(sprint_filter: str) -> str | None:
    """Return the sprint JQL clause for the given filter, or None if no clause needed."""
    if sprint_filter == "active":
        return "sprint in openSprints()"
    elif sprint_filter == "all":
        return None
    elif sprint_filter.startswith("named:"):
        name = sprint_filter[len("named:"):]
        escaped = name.replace('"', '\\"')
        return f'sprint = "{escaped}"'
    else:
        return "sprint in openSprints()"


def _passes_sprint_gate(sprint_value: str, cfg: Config) -> bool:
    """Return True if sprint_value satisfies the configured sprint filter."""
    f = cfg.jira_sprint_filter
    if f == "all":
        return True
    elif f == "active":
        if not sprint_value:
            return False
        if cfg.jira_active_sprint_name:
            return sprint_value == cfg.jira_active_sprint_name
        return True
    elif f.startswith("named:"):
        target = f[len("named:"):]
        return sprint_value == target
    else:
        return bool(sprint_value)


def ingest_jira_api(cfg: Config, repos: list[str]) -> None:
    """Query Jira API for sprint issues and create missing GH issues."""
    token = os.environ.get("JIRA_API_TOKEN")
    if not token or not cfg.jira_api_enabled:
        return

    try:
        client = JiraClient(cfg.jira_base_url, token)
        if not client.validate():
            logger.warning("Jira API authentication failed")
            return
    except Exception as e:
        logger.warning("Jira API validation error: %s", e)
        return

    logger.info("Jira API ingest: starting")

    # Build JQL to filter by configured projects and open sprints
    if not cfg.jira_projects:
        logger.warning("Jira API ingest: no projects configured (jira_projects), skipping")
        return

    projects_str = ", ".join(cfg.jira_projects)
    clauses = [f"project in ({projects_str})"]
    sprint_clause = _sprint_jql_clause(cfg.jira_sprint_filter)
    if sprint_clause:
        clauses.append(sprint_clause)
    jql = " AND ".join(clauses)

    try:
        issues = client.search(
            jql,
            fields=[
                "key", "summary", "status", "issuetype", "customfield_10005",  # Epic link
                "customfield_10006", "customfield_10007", "customfield_10008",  # Sprint variants
                "labels",  # For repo label routing
            ],
            max_results=1000,
        )
    except Exception as e:
        logger.warning("Jira API search failed: %s", e)
        return

    if not issues:
        logger.info("Jira API ingest: no issues in open sprints")
        return

    logger.info("Jira API ingest: processing %d issue(s)", len(issues))

    for issue in issues:
        try:
            jira_key = issue.get("key", "").strip()
            summary = issue.get("fields", {}).get("summary", "").strip()
            if not jira_key or not summary:
                continue

            # Skip by issue type
            issue_type = issue.get("fields", {}).get("issuetype", {}).get("name", "").lower()
            if issue_type in {t.lower() for t in cfg.skip_jira_issue_types}:
                log_decision(
                    "api_issue_skip",
                    jira_key=jira_key,
                    reason="excluded_type",
                    issue_type=issue_type,
                )
                logger.info(
                    "Jira API ingest: skipping %s (type=%s) per skip_jira_issue_types",
                    jira_key, issue_type,
                )
                continue

            # Extract sprint (hard gate — must have sprint)
            fields = issue.get("fields", {})
            sprint = None

            # Try customfield_10006 first (common Sprint field)
            sprint = fields.get("customfield_10006")
            if sprint and isinstance(sprint, list) and sprint:
                # Sprint field returns array of sprint objects
                sprint_obj = sprint[0]
                if isinstance(sprint_obj, dict):
                    sprint = sprint_obj.get("name", "").strip()
                else:
                    sprint = str(sprint_obj).strip()
            elif not sprint:
                # Try other sprint field variants
                for field_key in ("customfield_10007", "customfield_10008"):
                    sprint = fields.get(field_key)
                    if sprint:
                        if isinstance(sprint, list) and sprint:
                            sprint = sprint[0]
                        sprint = str(sprint).strip() if sprint else ""
                        if sprint:
                            break

            if not sprint:
                log_decision(
                    "api_issue_skip",
                    jira_key=jira_key,
                    reason="no_sprint",
                )
                logger.debug("Jira API ingest: skipping %s — no sprint assigned", jira_key)
                continue

            # Extract labels and filter repos (repo label routing)
            issue_labels = fields.get("labels", [])
            matching_repos = [r for r in repos if r in issue_labels]

            if not matching_repos:
                log_decision(
                    "api_issue_skip",
                    jira_key=jira_key,
                    reason="no_matching_repo_labels",
                    labels=issue_labels,
                )
                logger.debug(
                    "Jira API ingest: skipping %s — no matching repo labels (has: %s)",
                    jira_key, ", ".join(issue_labels) if issue_labels else "(none)",
                )
                continue

            # Build issue body
            row = {
                "issue key": jira_key,
                "summary": summary,
                "epic link": fields.get("customfield_10005", ""),
                "sprint": sprint,
            }
            body, missing = _build_issue_body(row, cfg.jira_base_url)
            conformant = len(missing) == 0

            title = f"[{jira_key}] {summary}"

            for repo in matching_repos:
                try:
                    if gh_issue_exists(repo, jira_key):
                        log_decision("api_issue_skip", repo=repo, jira_key=jira_key, reason="already_exists")
                        continue

                    issue_number = gh_issue_create(repo, title, body)

                    if issue_number is None:
                        logger.warning("Jira API ingest: failed to create issue for %s in %s", jira_key, repo)
                        continue

                    log_decision("api_issue_created", repo=repo, jira_key=jira_key, issue=issue_number, conformant=conformant)
                    label_jira_issue(repo, issue_number, title)
                    logger.info(
                        "Jira API ingest: created issue #%d for %s in %s%s",
                        issue_number, jira_key, repo,
                        " (non-conformant, not assigned)" if not conformant else "",
                    )

                    if not conformant:
                        needs_label = f"{cfg.github_user}:needs-jira-info"
                        try:
                            gh_ensure_label_exists(repo, needs_label, color="e11d48")
                            gh_issue_add_label(repo, issue_number, needs_label)
                        except Exception as e:
                            logger.warning("Jira API ingest: could not add needs-jira-info label: %s", e)

                except Exception as e:
                    logger.error("Jira API ingest: error processing %s in %s: %s", jira_key, repo, e)

        except Exception as e:
            logger.error("Jira API ingest: error processing issue %s: %s", jira_key, e)

    # Subtask pass — gated on cfg.ingest_subtasks
    if cfg.ingest_subtasks:
        for issue in issues:
            try:
                parent_jira_key = issue.get("key", "").strip()
                fields = issue.get("fields", {})
                subtasks = fields.get("subtasks", []) or []
                if not subtasks:
                    continue
                # Same label-based routing as the main pass
                issue_labels = fields.get("labels", [])
                matching_repos = [r for r in repos if r in issue_labels]
                if not matching_repos:
                    continue
                for subtask_stub in subtasks:
                    subtask_key = subtask_stub.get("key", "").strip()
                    if not subtask_key:
                        continue
                    subtask_summary = (subtask_stub.get("fields") or {}).get("summary", "").strip()
                    subtask_type = ((subtask_stub.get("fields") or {}).get("issuetype") or {}).get("name", "")
                    if not subtask_summary:
                        continue
                    subtask_title = f"[{subtask_key}] {subtask_summary}"
                    row = {
                        "issue key": subtask_key,
                        "summary": subtask_summary,
                        "issue type": subtask_type,
                        "epic link": fields.get("customfield_10005", "") or fields.get("customfield_10014", ""),
                        "sprint": "",  # subtasks inherit parent sprint context
                        "parent key": parent_jira_key,
                    }
                    body, _ = _build_issue_body(row, cfg.jira_base_url)
                    for repo in matching_repos:
                        try:
                            if gh_issue_exists(repo, subtask_key):
                                log_decision("api_issue_skip", repo=repo, jira_key=subtask_key, reason="already_exists")
                                continue
                            subtask_issue_number = gh_issue_create(repo, subtask_title, body)
                            if subtask_issue_number is None:
                                continue
                            label_jira_issue(repo, subtask_issue_number, subtask_title)
                            log_decision("api_subtask_created", repo=repo, jira_key=subtask_key,
                                         parent_jira_key=parent_jira_key, issue=subtask_issue_number)
                            logger.info("Jira API ingest: created subtask issue #%d for %s (parent: %s)",
                                        subtask_issue_number, subtask_key, parent_jira_key)
                            # Link as GitHub sub-issue
                            parent_gh_issue = _find_gh_issue_for_jira_key(repo, parent_jira_key)
                            if parent_gh_issue:
                                try:
                                    gh_add_sub_issue(repo, parent_gh_issue, subtask_issue_number)
                                    log_decision("api_sub_issue_linked", repo=repo,
                                                 parent_issue=parent_gh_issue, sub_issue=subtask_issue_number)
                                except Exception as link_err:
                                    logger.warning("Could not link sub-issue %d to parent %d: %s",
                                                   subtask_issue_number, parent_gh_issue, link_err)
                            else:
                                log_decision("api_subtask_skip_no_parent", repo=repo,
                                             jira_key=subtask_key, parent_jira_key=parent_jira_key)
                        except Exception as e:
                            logger.error("Jira API ingest: error processing subtask %s: %s", subtask_key, e)
            except Exception as e:
                logger.error("Jira API ingest: subtask pass error for %s: %s", issue.get("key"), e)

    logger.info("Jira API ingest: complete")


def _run_jira_ingest(cfg: Config, repos: list[str]) -> None:
    """Run ingest_jira_api with circuit-breaker protection."""
    global _jira_consecutive_failures, _jira_backoff_until

    if time.monotonic() < _jira_backoff_until:
        logger.debug("Jira API in backoff — skipping ingest")
        return

    try:
        ingest_jira_api(cfg, repos)
        if _jira_consecutive_failures > 0:
            logger.info("Jira API circuit closed after %d failure(s)", _jira_consecutive_failures)
            log_decision("jira_circuit_closed", consecutive_failures=_jira_consecutive_failures)
        _jira_consecutive_failures = 0
    except Exception as e:
        _jira_consecutive_failures += 1
        if _jira_consecutive_failures >= cfg.jira_failure_threshold:
            delay = min(
                cfg.jira_backoff_base * (2 ** (_jira_consecutive_failures - cfg.jira_failure_threshold)),
                cfg.jira_backoff_max,
            )
            _jira_backoff_until = time.monotonic() + delay
            logger.warning(
                "Jira API circuit open (%d consecutive failures) — backing off %ds",
                _jira_consecutive_failures, delay,
            )
            log_decision(
                "jira_circuit_open",
                consecutive_failures=_jira_consecutive_failures,
                backoff_seconds=delay,
            )
        raise


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
        _clean_hunter_labels(repo, issue_number, cfg)

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

        if status in ("in_progress", "specifying"):
            # Was mid-proposal-creation — check if worktree has commits
            wt = entry.get("proposal_worktree")
            if wt and Path(wt).exists():
                base = entry.get("base_branch", "main")
                if worktree_has_commits_since(Path(wt), base):
                    logger.info("Resuming %s from %s — worktree has commits, checking for existing PR", key, status)
                    # Check if a PR already exists with our marker
                    marker = f"hunter:issue-{issue_number}"
                    try:
                        prs = gh_list_prs_with_marker(repo, marker)
                        if prs:
                            pr_number = prs[0]["number"]
                            resume_status = "spec_open" if entry.get("used_speckit") else "proposal_open"
                            logger.info("Found existing proposal PR #%d for %s — advancing to %s",
                                        pr_number, key, resume_status)
                            update_issue_state(state, key,
                                               status=resume_status,
                                               proposal_pr=pr_number,
                                               resume_attempts=resume_attempts + 1)
                        else:
                            logger.info("No PR found for %s — incrementing resume_attempts", key)
                            update_issue_state(state, key, resume_attempts=resume_attempts + 1)
                    except Exception as e:
                        logger.warning("Could not check PRs for %s: %s", key, e)
                else:
                    rollback_issue(cfg, state, key, f"{status} with no commits in worktree")
            else:
                rollback_issue(cfg, state, key, f"{status} with no worktree")

        elif status in ("proposal_open", "spec_open"):
            # Check proposal PR still exists
            proposal_pr = entry.get("proposal_pr")
            if not proposal_pr:
                rollback_issue(cfg, state, key, f"{status} with no proposal_pr")
            # Otherwise fine — poll loop will check merge status

        elif status in ("implementing", "self_reviewing"):
            wt = entry.get("impl_worktree")
            # Retry a failed push from a previous self-review fix loop
            if entry.get("impl_push_failed") and entry.get("impl_pr"):
                if wt and Path(wt).exists():
                    logger.info("Retrying push for %s after previous push failure", key)
                    try:
                        subprocess.run(
                            ["git", "push"],
                            cwd=wt, check=True, capture_output=True,
                        )
                        update_issue_state(state, key,
                                           impl_push_failed=False,
                                           resume_attempts=resume_attempts + 1)
                        logger.info("Push retry succeeded for %s", key)
                    except subprocess.CalledProcessError as push_err:
                        logger.warning("Push retry failed for %s: %s — falling through to rollback", key, push_err)
                        rollback_issue(cfg, state, key, "impl_push_failed and retry also failed")
                else:
                    rollback_issue(cfg, state, key, "impl_push_failed but worktree missing")
                continue
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
                    # Roll back so we re-run impl from scratch
                    reset_status = "spec_open" if entry.get("used_speckit") else "proposal_open"
                    logger.warning("Resetting %s to %s — impl worktree missing", key, reset_status)
                    update_issue_state(state, key,
                                       status=reset_status,
                                       impl_pr=None,
                                       impl_worktree=None,
                                       resume_attempts=resume_attempts + 1)

        elif status == "ready_for_review":
            impl_pr = entry.get("impl_pr")
            if not impl_pr:
                rollback_issue(cfg, state, key, "ready_for_review with no impl_pr")

        elif status == "failed":
            # Failed issues retry up to max_resume_retries times before rollback
            logger.info("Retrying failed issue %s (attempt %d/%d)", key, resume_attempts + 1, cfg.max_resume_retries)
            update_issue_state(state, key, resume_attempts=resume_attempts + 1)


def scan_orphaned_labels(cfg: Config, state: dict, repos: list[str]) -> None:
    """
    Remove hunter-owned labels from issues that have no matching hunter state entry
    or are in a terminal state. Runs at startup and periodically.
    """
    labels_to_check = [
        f"{cfg.github_user}:in-progress",
        f"{cfg.github_user}:proposal-open",
        f"{cfg.github_user}:spec-open",
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
    r"^(\[[A-Z][A-Z0-9]+-\d+\]\s*)?(proposal|propose|sdd|design|rfc|spec)[:\s\-]", re.IGNORECASE)
_IMPL_TITLE_RE = re.compile(
    r"^(\[[A-Z][A-Z0-9]+-\d+\]\s*)?(impl|implement|feat|fix|chore)[:\s\-]", re.IGNORECASE)


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

    hunter_repos = cfg.repos_for("hunter")

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

            # Ingest new issues from Jira API (if configured)
            try:
                _run_jira_ingest(cfg, hunter_repos)
            except Exception as e:
                logger.warning("Jira API ingest failed: %s", e)

            # Auto-label unlabelled proposal/implementation PRs
            auto_label_prs(cfg, hunter_repos)

            # Resume or rollback any in-flight issues from previous runs
            resume_in_flight_issues(cfg, state)
            state = load_hunter_state()
            reconcile_assigned_issues(cfg, state, hunter_repos)
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

                    # Only work on issues with the "jira" label
                    label_names = [lbl["name"] for lbl in issue.get("labels", [])]
                    if "jira" not in label_names:
                        log_decision("issue_skip", repo=repo, issue=issue_number, reason="no_jira_label")
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
                        if status in ("proposal_open", "spec_open"):
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
            state = load_hunter_state()
            _cleanup_ephemeral_states(state)
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
    else:
        for s, c in sorted(counts.items()):
            click.echo(f"{s}: {c}")

    cfg = load_config()
    if cfg.jira_api_enabled:
        remaining = _jira_backoff_until - time.monotonic()
        if remaining > 0:
            click.echo(
                f"Jira: circuit open ({_jira_consecutive_failures} consecutive failures,"
                f" backoff {int(remaining)}s remaining)"
            )
        else:
            click.echo("Jira: OK")


@cli.command(name="init")
@click.option("--force", is_flag=True, help="Overwrite existing config without prompting.")
@click.option("--ui", is_flag=True, help="Serve a web UI instead of terminal wizard.")
def init_cmd(force: bool, ui: bool):
    """Interactive config wizard (alias for predd init)."""
    if ui:
        click.echo("Web UI not yet implemented.")
        return
    _predd.run_config_wizard(force=force)


if __name__ == "__main__":
    cli()
