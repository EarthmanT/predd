#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "click",
# ]
# ///

"""
sentinel.py — post-CI review of hunter-created PRs.

After predd reviews and posts its comments on a PR, sentinel runs a
second pass once CI has finished: it fetches the diff and workflow logs,
runs an LLM skill, and files any findings as new GitHub issues so they
flow back into the hunter pipeline.
"""

import hashlib
import importlib.util
import json
import logging
import re
import subprocess
from pathlib import Path

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
gh_run = _predd.gh_run
gh_issue_comment = _predd.gh_issue_comment
gh_issue_add_label = _predd.gh_issue_add_label
_run_bedrock_skill = _predd._run_bedrock_skill
_now_iso = _predd._now_iso
log_decision = _predd.log_decision

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = logging.getLogger("sentinel")

# ---------------------------------------------------------------------------
# CI check-run helpers
# ---------------------------------------------------------------------------

_TERMINAL_CONCLUSIONS = {
    "success", "failure", "cancelled", "timed_out",
    "action_required", "neutral", "skipped",
}


def _fetch_check_runs(repo: str, sha: str) -> list[dict]:
    """Return all check runs for a commit SHA via gh api."""
    result = gh_run(
        ["api", f"repos/{repo}/commits/{sha}/check-runs", "--jq", ".check_runs[]"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    runs = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return runs


def _ci_is_finished(check_runs: list[dict]) -> bool:
    """True if all check runs have a terminal conclusion (not queued/in_progress)."""
    if not check_runs:
        # No CI configured — treat as finished so sentinel can still run.
        return True
    for run in check_runs:
        status = run.get("status", "")
        conclusion = run.get("conclusion") or ""
        if status != "completed":
            return False
        if conclusion not in _TERMINAL_CONCLUSIONS:
            return False
    return True


# ---------------------------------------------------------------------------
# Workflow log fetcher
# ---------------------------------------------------------------------------

def _fetch_workflow_logs(repo: str, pr_number: int) -> str:
    """Fetch logs for all completed workflow runs for this PR.

    Returns concatenated string, max ~50 000 chars total.
    """
    result = gh_run(
        [
            "run", "list",
            "--repo", repo,
            "--event", "pull_request",
            "--json", "databaseId,conclusion,name",
            "--limit", "20",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""

    try:
        runs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""

    MAX_TOTAL = 50_000
    MAX_JOB_LINES = 200
    parts: list[str] = []
    total_chars = 0

    for run_entry in runs:
        if run_entry.get("conclusion") in (None, ""):
            # Still running
            continue
        run_id = run_entry.get("databaseId")
        if not run_id:
            continue

        log_result = gh_run(
            ["run", "view", str(run_id), "--repo", repo, "--log"],
            check=False,
        )
        if log_result.returncode != 0:
            continue

        # Truncate per-job log to last MAX_JOB_LINES lines
        lines = log_result.stdout.splitlines()
        if len(lines) > MAX_JOB_LINES:
            lines = lines[-MAX_JOB_LINES:]
        chunk = f"=== Run {run_id} ({run_entry.get('name', '')}) ===\n" + "\n".join(lines)

        if total_chars + len(chunk) > MAX_TOTAL:
            remaining = MAX_TOTAL - total_chars
            if remaining > 0:
                parts.append(chunk[:remaining])
            break

        parts.append(chunk)
        total_chars += len(chunk)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Fingerprinting & dedup
# ---------------------------------------------------------------------------

def _fingerprint(source_pr: int, title: str, source: str) -> str:
    """SHA256-based 16-char fingerprint for dedup."""
    return hashlib.sha256(
        f"{source_pr}:{title}:{source}".encode()
    ).hexdigest()[:16]


def _already_filed(repo: str, fp: str, github_user: str) -> bool:
    """Check if an issue with this fingerprint already exists."""
    result = gh_run(
        [
            "issue", "list",
            "--repo", repo,
            "--state", "all",
            "--search", f"sentinel-fingerprint: {fp}",
            "--json", "number",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return False
    try:
        issues = json.loads(result.stdout)
        return len(issues) > 0
    except json.JSONDecodeError:
        return False


def _open_auto_filed_count(repo: str, github_user: str) -> int:
    """Count open issues with the auto-filed label."""
    label = f"{github_user}:auto-filed"
    result = gh_run(
        [
            "issue", "list",
            "--repo", repo,
            "--state", "open",
            "--label", label,
            "--json", "number",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return 0
    try:
        issues = json.loads(result.stdout)
        return len(issues)
    except json.JSONDecodeError:
        return 0


# ---------------------------------------------------------------------------
# Filing issues
# ---------------------------------------------------------------------------

_ISSUE_BODY_TEMPLATE = """\
**Source:** PR #{source_pr}
**Severity:** {severity}
**Detected by:** sentinel (post-CI review)

## What

{rationale}

## Where

{source}

## Suggested fix

{suggested_fix}

---
<!-- sentinel-fingerprint: {fingerprint} -->
<!-- sentinel-source-pr: {source_pr} -->
"""


def _file_finding(cfg: Config, repo: str, source_pr: int, finding: dict) -> "int | None":
    """File a single finding as a GitHub issue. Returns issue number or None."""
    title = finding.get("title", "").strip()
    severity = finding.get("severity", "concern")
    source = finding.get("source", "")
    rationale = finding.get("rationale", "")
    suggested_fix = finding.get("suggested_fix", "")

    if not title:
        logger.warning("Sentinel: finding missing title, skipping")
        return None

    fp = _fingerprint(source_pr, title, source)

    # Dedup check
    if _already_filed(repo, fp, cfg.github_user):
        logger.info("Sentinel: finding already filed (fingerprint %s), skipping", fp)
        log_decision(
            "post_ci_finding_skipped",
            repo=repo,
            pr=source_pr,
            title=title,
            severity=severity,
            fingerprint=fp,
            reason="duplicate",
        )
        return None

    # Backpressure check
    open_count = _open_auto_filed_count(repo, cfg.github_user)
    if open_count >= cfg.max_open_auto_issues:
        logger.info(
            "Sentinel: at cap (%d/%d open auto-filed issues), deferring finding: %s",
            open_count,
            cfg.max_open_auto_issues,
            title,
        )
        log_decision(
            "post_ci_finding_deferred",
            repo=repo,
            pr=source_pr,
            title=title,
            severity=severity,
            fingerprint=fp,
        )
        return None

    body = _ISSUE_BODY_TEMPLATE.format(
        source_pr=source_pr,
        severity=severity,
        rationale=rationale,
        source=source,
        suggested_fix=suggested_fix,
        fingerprint=fp,
    )

    # Create issue
    create_args = [
        "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
        "--label", f"{cfg.github_user}:auto-filed",
        "--label", f"auto-filed:{severity}",
    ]
    if cfg.auto_assign_filed_issues:
        create_args += ["--assignee", cfg.github_user]

    result = gh_run(create_args, check=False)
    if result.returncode != 0:
        logger.error("Sentinel: failed to create issue for finding '%s': %s", title, result.stderr)
        return None

    # Parse issue number from URL output (gh prints the URL on success)
    issue_number: int | None = None
    for line in result.stdout.strip().splitlines():
        m = re.search(r"/issues/(\d+)", line)
        if m:
            issue_number = int(m.group(1))
            break

    logger.info("Sentinel: filed finding '%s' as issue #%s (severity=%s)", title, issue_number, severity)
    log_decision(
        "post_ci_finding_filed",
        repo=repo,
        pr=source_pr,
        issue=issue_number,
        title=title,
        severity=severity,
        fingerprint=fp,
    )
    return issue_number


# ---------------------------------------------------------------------------
# LLM skill runner
# ---------------------------------------------------------------------------

def _strip_yaml_frontmatter(text: str) -> str:
    """Strip YAML frontmatter (---...---) from start of text."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def _strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` fences wrapping JSON."""
    text = text.strip()
    # Remove leading fence
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    # Remove trailing fence
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _run_review_skill(cfg: Config, prompt: str) -> str:
    """Run the post-CI skill via the configured backend. Returns raw text output."""
    skill_path = cfg.post_ci_skill_path

    if cfg.backend == "bedrock":
        # _run_bedrock_skill needs a worktree path; pass a temp dir (no git ops needed)
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            return _run_bedrock_skill(cfg, prompt, skill_path, Path(tmpdir))

    if cfg.backend == "claude":
        import os
        skill_body = _strip_yaml_frontmatter(skill_path.read_text()) if skill_path.exists() else ""
        full_prompt = f"{prompt}\n\n---\n\n{skill_body}" if skill_body else prompt
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        result = subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions", "--model", cfg.model],
            input=full_prompt,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude exited {result.returncode}: {result.stderr[:500]}")
        return result.stdout

    # devin
    import os
    _DEVIN_STRIP_ENV = _predd._DEVIN_STRIP_ENV
    skill_body = _strip_yaml_frontmatter(skill_path.read_text()) if skill_path.exists() else ""
    full_prompt = f"{prompt}\n\n---\n\n{skill_body}" if skill_body else prompt
    env = {k: v for k, v in os.environ.items() if k not in _DEVIN_STRIP_ENV}
    result = subprocess.run(
        ["setsid", "devin", "-p", "--permission-mode", "auto", "--model", cfg.model, "--", full_prompt],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"devin exited {result.returncode}: {result.stderr[:500]}")
    return result.stdout


def _parse_findings(raw: str) -> list[dict]:
    """Extract findings list from LLM output. Returns empty list on any parse error."""
    text = _strip_markdown_fences(raw)
    # Try to find a JSON object anywhere in the output
    for match in re.finditer(r"\{", text):
        candidate = text[match.start():]
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and "findings" in data:
                findings = data["findings"]
                if isinstance(findings, list):
                    return findings
        except json.JSONDecodeError:
            continue
    # Maybe the whole text is a JSON array
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    logger.warning("Sentinel: could not parse findings JSON from skill output")
    return []


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _is_hunter_pr(pr_data: dict, cfg: Config) -> bool:
    """Return True if this PR appears to be authored by hunter."""
    head_ref = pr_data.get("headRefName", "")
    labels = [lb.get("name", "") for lb in pr_data.get("labels", [])]
    author_login = pr_data.get("author", {}).get("login", "")

    if head_ref.startswith(cfg.branch_prefix):
        return True
    if "sdd-proposal" in labels or "sdd-implementation" in labels:
        return True
    if author_login == cfg.github_user:
        return True
    return False


def run_post_ci_review(cfg: Config, state: dict, repo: str, pr_number: int) -> None:
    """Main entry point. Check trigger conditions, run LLM, file findings."""
    key = f"{repo}#{pr_number}"
    entry = state.get(key, {})

    # 1. Not already post-CI-reviewed
    if entry.get("post_ci_reviewed"):
        logger.debug("Sentinel: %s already post-CI-reviewed, skipping", key)
        return

    # 2. Fetch PR metadata to check hunter authorship
    pr_result = gh_run(
        [
            "pr", "view", str(pr_number),
            "--repo", repo,
            "--json", "number,title,body,headRefName,headRefOid,labels,author",
        ],
        check=False,
    )
    if pr_result.returncode != 0:
        logger.warning("Sentinel: could not fetch PR %s: %s", key, pr_result.stderr)
        return

    try:
        pr_data = json.loads(pr_result.stdout)
    except json.JSONDecodeError:
        logger.warning("Sentinel: invalid JSON from pr view for %s", key)
        return

    if not _is_hunter_pr(pr_data, cfg):
        logger.debug("Sentinel: %s is not a hunter PR, skipping", key)
        return

    head_sha = pr_data.get("headRefOid", "")

    # 3. Check CI is finished
    check_runs = _fetch_check_runs(repo, head_sha)
    if not _ci_is_finished(check_runs):
        logger.debug("Sentinel: CI not finished for %s, skipping this cycle", key)
        return

    logger.info("Sentinel: running post-CI review for %s", key)
    log_decision("post_ci_review_started", repo=repo, pr=pr_number)

    # 4. Fetch PR diff
    diff_result = gh_run(["pr", "diff", str(pr_number), "--repo", repo], check=False)
    diff = diff_result.stdout if diff_result.returncode == 0 else "(diff unavailable)"

    # 5. Fetch workflow logs
    logs = _fetch_workflow_logs(repo, pr_number)

    # 6. Build prompt for skill
    pr_title = pr_data.get("title", "")
    pr_body = pr_data.get("body", "") or ""
    prompt = (
        f"Review the following hunter-created PR for CI findings.\n\n"
        f"PR: #{pr_number} — {pr_title}\n"
        f"Repo: {repo}\n\n"
        f"PR Description:\n{pr_body[:2000]}\n\n"
        f"--- DIFF ---\n{diff[:20000]}\n\n"
        f"--- WORKFLOW LOGS ---\n{logs[:20000]}\n\n"
        f"Return ONLY a JSON object with a 'findings' array. Each finding must have:\n"
        f"  title, severity (blocker|concern|nit), source, rationale, suggested_fix\n"
        f"If there are no findings, return {{\"findings\": []}}\n"
    )

    # 7. Run LLM skill
    try:
        raw_output = _run_review_skill(cfg, prompt)
    except Exception as e:
        logger.error("Sentinel: skill failed for %s: %s", key, e)
        log_decision("post_ci_review_failed", repo=repo, pr=pr_number, error=str(e))
        # Mark as reviewed so we don't keep retrying a broken skill
        entry["post_ci_reviewed"] = True
        entry["post_ci_reviewed_at"] = _now_iso()
        state[key] = entry
        save_state(state)
        return

    # 8. Parse findings
    findings = _parse_findings(raw_output)

    # 9. File blocker/concern findings; log nits
    filed = 0
    deferred = 0

    for finding in findings:
        severity = finding.get("severity", "nit")
        title = finding.get("title", "")

        if severity == "nit":
            log_decision(
                "post_ci_finding_skipped",
                repo=repo,
                pr=pr_number,
                title=title,
                severity=severity,
                fingerprint=_fingerprint(pr_number, title, finding.get("source", "")),
                reason="nit",
            )
            continue

        if severity not in ("blocker", "concern"):
            # Unknown severity — skip
            continue

        # Count as deferred before filing so _file_finding can recount accurately
        issue_num = _file_finding(cfg, repo, pr_number, finding)
        if issue_num is not None:
            filed += 1
        else:
            # Could be duplicate or deferred
            fp = _fingerprint(pr_number, title, finding.get("source", ""))
            if not _already_filed(repo, fp, cfg.github_user):
                deferred += 1

    # 10. Mark state
    entry["post_ci_reviewed"] = True
    entry["post_ci_reviewed_at"] = _now_iso()
    entry["post_ci_findings_filed"] = filed
    entry["post_ci_findings_deferred"] = deferred
    state[key] = entry
    save_state(state)

    log_decision(
        "post_ci_review_completed",
        repo=repo,
        pr=pr_number,
        findings_filed=filed,
        findings_deferred=deferred,
    )
    logger.info("Sentinel: completed for %s — filed=%d deferred=%d", key, filed, deferred)
