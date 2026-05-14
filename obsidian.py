#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "click",
#   "anthropic[bedrock]",
# ]
# ///

import importlib.util
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import threading
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
_now_iso = _predd._now_iso

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_DIR = Path("~/.config/predd").expanduser()
OBSIDIAN_DIR = CONFIG_DIR / "obsidian"
OBSIDIAN_OBSERVATIONS_DIR = OBSIDIAN_DIR / "observations"
OBSIDIAN_ANALYSIS_DIR = OBSIDIAN_DIR / "analysis"
OBSIDIAN_PATTERNS_DIR = OBSIDIAN_DIR / "patterns"
OBSIDIAN_PID_FILE = CONFIG_DIR / "obsidian-pid"
OBSIDIAN_LOG_FILE = CONFIG_DIR / "obsidian-log.txt"
OBSIDIAN_LAST_OBSERVE = OBSIDIAN_DIR / ".last-observe"
HUNTER_DECISION_LOG = CONFIG_DIR / "hunter-decisions.jsonl"
HUNTER_STATE_FILE = CONFIG_DIR / "hunter-state.json"
DECISION_LOG = CONFIG_DIR / "decisions.jsonl"

# Where obsidian.py lives — used to find spec/changes/ relative to this repo
_REPO_DIR = Path(__file__).resolve().parent
SPEC_CHANGES_DIR = _REPO_DIR / "spec" / "changes"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging() -> logging.Logger:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("obsidian")
    logger.setLevel(logging.DEBUG)
    handler = logging.handlers.RotatingFileHandler(
        OBSIDIAN_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(stderr_handler)
    return logger


logger = logging.getLogger("obsidian")

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
    if OBSIDIAN_PID_FILE.exists():
        try:
            pid = int(OBSIDIAN_PID_FILE.read_text().strip())
            if _pid_alive(pid):
                click.echo(f"obsidian already running (PID {pid}). Exiting.", err=True)
                sys.exit(1)
        except ValueError:
            pass
    OBSIDIAN_PID_FILE.write_text(str(os.getpid()))


def release_pid_file() -> None:
    try:
        OBSIDIAN_PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_stop = threading.Event()


def _shutdown(signum, frame):
    if _stop.is_set():
        release_pid_file()
        sys.exit(1)
    _stop.set()
    logger.info("Shutting down after current task (^C again to force quit)...")


# ---------------------------------------------------------------------------
# Subprocess runner (for _run_claude)
# ---------------------------------------------------------------------------

_active_proc_obsidian = None


def _run_proc_obsidian(cmd: list[str], worktree: Path, env: dict | None = None,
                       stdin_text: str | None = None) -> str:
    import subprocess
    global _active_proc_obsidian
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        text=True,
        cwd=str(worktree),
        env=env,
    )
    _active_proc_obsidian = proc
    try:
        stdout, stderr_out = proc.communicate(input=stdin_text, timeout=900)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        _active_proc_obsidian = None
    if proc.returncode != 0:
        import subprocess as _sp
        raise _sp.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr_out)
    return stdout


def _run_claude(cfg: Config, prompt: str, worktree: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    return _run_proc_obsidian(
        ["claude", "-p", "--dangerously-skip-permissions", "--model", cfg.model],
        worktree, env=env, stdin_text=prompt,
    )


def _run_bedrock_text(cfg: Config, prompt: str) -> str:
    """Call Bedrock for a plain text response — no tools, no skill file."""
    try:
        from anthropic import AnthropicBedrock
    except ImportError:
        raise RuntimeError("anthropic[bedrock] not installed. Run: pip install -U 'anthropic[bedrock]'")

    if cfg.aws_profile and cfg.aws_profile != "default":
        os.environ["AWS_PROFILE"] = cfg.aws_profile

    client = AnthropicBedrock(aws_region=cfg.aws_region)
    resp = client.messages.create(
        model=cfg.bedrock_model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(
        block.text for block in resp.content if hasattr(block, "text") and block.text
    )


def _run_llm(cfg: Config, prompt: str, worktree: Path) -> str:
    """Dispatch to the configured backend for a plain text LLM call."""
    if cfg.backend == "claude":
        return _run_claude(cfg, prompt, worktree)
    if cfg.backend == "bedrock":
        return _run_bedrock_text(cfg, prompt)
    raise ValueError(f"Unknown backend '{cfg.backend}'. Valid values: claude, bedrock")


# ---------------------------------------------------------------------------
# Observe helpers
# ---------------------------------------------------------------------------


def _load_last_observe() -> str | None:
    """Read OBSIDIAN_LAST_OBSERVE. Returns ISO string or None if missing."""
    try:
        if OBSIDIAN_LAST_OBSERVE.exists():
            return OBSIDIAN_LAST_OBSERVE.read_text().strip() or None
    except OSError:
        pass
    return None


def _save_last_observe(ts: str) -> None:
    """Write timestamp to OBSIDIAN_LAST_OBSERVE."""
    OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)
    OBSIDIAN_LAST_OBSERVE.write_text(ts)


def _read_jsonl_since(path: Path, since: str | None) -> list[dict]:
    """Read JSONL file, filter to records with ts >= since. Skips corrupt lines."""
    if not path.exists():
        return []
    records = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if since is not None:
                    ts = record.get("ts", "")
                    if ts < since:
                        continue
                records.append(record)
    except OSError:
        pass
    return records


def _load_hunter_state_file() -> dict:
    """Read hunter-state.json safely. Returns empty dict on error."""
    if not HUNTER_STATE_FILE.exists():
        return {}
    try:
        return json.loads(HUNTER_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _build_observations(
    hunter_state: dict,
    hunter_events: list[dict],
    predd_events: list[dict],
    since: str | None,
) -> list[dict]:
    """
    Aggregate data into observation dicts.

    Returns a list of dicts, each with:
      - type: "pr" or "issue"
      - number: int
      - repo: str
      - title: str
      - label: str (sdd-proposal / sdd-implementation / "")
      - issue_number: int | None (for PR observations)
      - status: str (for issue observations)
      - reviews: list[dict]  (for PR obs)
      - comments: list[dict] (for PR obs)
      - feedback_summary: list[str] (for issue obs)
      - related_pr: int | None (for issue obs)
      - observed_at: str
    """
    observations = []
    now = _now_iso()

    # Build PR observations from hunter state entries that have proposal_feedback or impl_feedback
    seen_pr_keys: set[str] = set()
    seen_issue_keys: set[str] = set()

    for key, entry in hunter_state.items():
        repo = entry.get("repo", "")
        issue_number = entry.get("issue_number")
        title = entry.get("title", "")
        status = entry.get("status", "")

        # -- Proposal PR observation --
        proposal_pr = entry.get("proposal_pr")
        proposal_feedback = entry.get("proposal_feedback", [])
        # Filter feedback by since
        if since:
            proposal_feedback = [f for f in proposal_feedback if f.get("ts", "") >= since]

        if proposal_pr and proposal_feedback:
            pr_key = f"{repo}#{proposal_pr}"
            if pr_key not in seen_pr_keys:
                seen_pr_keys.add(pr_key)
                reviews = []
                comments = []
                for fb in proposal_feedback:
                    fb_type = fb.get("type", "COMMENT")
                    reviewer = fb.get("reviewer", "")
                    ts = fb.get("ts", now)
                    body = fb.get("body", "")
                    inline = fb.get("inline_comments", [])
                    if fb_type in ("APPROVED", "REQUEST_CHANGES", "COMMENTED", "APPROVE",
                                   "REQUEST_CHANGES"):
                        reviews.append({
                            "state": fb_type,
                            "reviewer": reviewer,
                            "ts": ts,
                            "body": body,
                            "inline_comments": inline,
                        })
                    else:
                        comments.append({
                            "reviewer": reviewer,
                            "ts": ts,
                            "body": body,
                        })

                observations.append({
                    "type": "pr",
                    "number": proposal_pr,
                    "repo": repo,
                    "title": f"Proposal: {title}",
                    "label": "sdd-proposal",
                    "issue_number": issue_number,
                    "reviews": reviews,
                    "comments": comments,
                    "observed_at": now,
                })

        # -- Impl PR observation --
        impl_pr = entry.get("impl_pr")
        impl_feedback = entry.get("impl_feedback", [])
        if since:
            impl_feedback = [f for f in impl_feedback if f.get("ts", "") >= since]

        if impl_pr and impl_feedback:
            pr_key = f"{repo}#{impl_pr}"
            if pr_key not in seen_pr_keys:
                seen_pr_keys.add(pr_key)
                reviews = []
                comments = []
                for fb in impl_feedback:
                    fb_type = fb.get("type", "COMMENT")
                    reviewer = fb.get("reviewer", "")
                    ts = fb.get("ts", now)
                    body = fb.get("body", "")
                    inline = fb.get("inline_comments", [])
                    if fb_type in ("APPROVED", "REQUEST_CHANGES", "COMMENTED", "APPROVE"):
                        reviews.append({
                            "state": fb_type,
                            "reviewer": reviewer,
                            "ts": ts,
                            "body": body,
                            "inline_comments": inline,
                        })
                    else:
                        comments.append({
                            "reviewer": reviewer,
                            "ts": ts,
                            "body": body,
                        })

                observations.append({
                    "type": "pr",
                    "number": impl_pr,
                    "repo": repo,
                    "title": f"Implement: {title}",
                    "label": "sdd-implementation",
                    "issue_number": issue_number,
                    "reviews": reviews,
                    "comments": comments,
                    "observed_at": now,
                })

        # -- Issue observation --
        # Build issue observation if there is any feedback activity
        any_feedback = bool(proposal_feedback) or bool(
            [f for f in entry.get("impl_feedback", [])
             if not since or f.get("ts", "") >= since]
        )
        if any_feedback and issue_number:
            issue_key = f"{repo}!{issue_number}"
            if issue_key not in seen_issue_keys:
                seen_issue_keys.add(issue_key)
                # Collect feedback summary bullets
                feedback_items = list(proposal_feedback) + [
                    f for f in entry.get("impl_feedback", [])
                    if not since or f.get("ts", "") >= since
                ]
                feedback_summary = []
                for fb in feedback_items:
                    body = fb.get("body", "").strip()
                    if body:
                        feedback_summary.append(body[:200])
                    for ic in fb.get("inline_comments", []):
                        ic_body = ic.get("body", "").strip()
                        if ic_body:
                            feedback_summary.append(f"`{ic.get('path','')}` — {ic_body[:200]}")

                related_pr = proposal_pr or impl_pr

                observations.append({
                    "type": "issue",
                    "number": issue_number,
                    "repo": repo,
                    "title": title,
                    "status": status,
                    "related_pr": related_pr,
                    "feedback_summary": feedback_summary,
                    "observed_at": now,
                })

    # Also capture predd-reviewed PRs from decisions.jsonl
    for event in predd_events:
        if event.get("event") != "pr_review_posted":
            continue
        repo = event.get("repo", "")
        pr_number = event.get("pr")
        if not pr_number:
            continue
        pr_key = f"{repo}#{pr_number}"
        if pr_key in seen_pr_keys:
            continue
        seen_pr_keys.add(pr_key)
        observations.append({
            "type": "pr",
            "number": pr_number,
            "repo": repo,
            "title": f"PR #{pr_number}",
            "label": "",
            "issue_number": None,
            "reviews": [{
                "state": event.get("verdict", "UNKNOWN"),
                "reviewer": "predd",
                "ts": event.get("ts", now),
                "body": "",
                "inline_comments": [],
            }],
            "comments": [],
            "observed_at": now,
        })

    return observations


def _write_observation_note(obs: dict, dry_run: bool = False) -> str:
    """
    Write an observation note for a PR or issue.
    Returns the path it would write to (as string).
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    obs_type = obs["type"]
    number = obs["number"]

    if obs_type == "pr":
        filename = f"{date_str}-pr-{number}.md"
    else:
        filename = f"{date_str}-issue-{number}.md"

    note_path = OBSIDIAN_OBSERVATIONS_DIR / filename

    if obs_type == "pr":
        lines = [
            "---",
            f"type: pr-observation",
            f"pr: {number}",
            f"issue: {obs.get('issue_number', '')}",
            f"repo: {obs['repo']}",
            f'title: "{obs["title"]}"',
            f"label: {obs.get('label', '')}",
            f"observed_at: {obs['observed_at']}",
            "---",
            "",
        ]

        reviews = obs.get("reviews", [])
        if reviews:
            lines.append("## Reviews")
            lines.append("")
            for r in reviews:
                lines.append(f"### {r['state']} — {r['reviewer']} ({r['ts']})")
                if r.get("body"):
                    lines.append(r["body"])
                inlines = r.get("inline_comments", [])
                if inlines:
                    lines.append("")
                    lines.append("**Inline comments:**")
                    for ic in inlines:
                        path = ic.get("path", "")
                        line_num = ic.get("line", "")
                        ic_body = ic.get("body", "")
                        loc = f"{path}:{line_num}" if line_num else path
                        lines.append(f"- `{loc}` — {ic_body}")
                lines.append("")

        comments = obs.get("comments", [])
        if comments:
            lines.append("## Comments")
            lines.append("")
            for c in comments:
                lines.append(
                    f"- **{c['reviewer']}** ({c['ts']}): {c['body']}"
                )
            lines.append("")

    else:  # issue
        status = obs.get("status", "")
        related_pr = obs.get("related_pr")
        feedback_summary = obs.get("feedback_summary", [])

        lines = [
            "---",
            f"type: issue-observation",
            f"issue: {number}",
            f"repo: {obs['repo']}",
            f'title: "{obs["title"]}"',
            f"status: {status}",
            f"observed_at: {obs['observed_at']}",
            "---",
            "",
        ]

        lines.append("## Current State")
        if related_pr:
            lines.append(f"Related PR #{related_pr}. Status: {status}.")
        else:
            lines.append(f"Status: {status}.")
        lines.append("")

        if feedback_summary:
            lines.append("## Feedback Summary")
            for item in feedback_summary:
                lines.append(f"- {item}")
            lines.append("")

        if related_pr:
            lines.append("## Related")
            lines.append(f"- [[observations/{date_str}-pr-{related_pr}]]")
            lines.append("")

    content = "\n".join(lines)

    if not dry_run:
        OBSIDIAN_OBSERVATIONS_DIR.mkdir(parents=True, exist_ok=True)
        note_path.write_text(content)

    return str(note_path)


# ---------------------------------------------------------------------------
# run_observe
# ---------------------------------------------------------------------------


def run_observe(cfg: Config, since: str | None = None, dry_run: bool = False) -> None:
    OBSIDIAN_OBSERVATIONS_DIR.mkdir(parents=True, exist_ok=True)

    effective_since = since if since is not None else _load_last_observe()
    now = _now_iso()

    obsidian_repos: set[str] = set(cfg.repos_for("obsidian"))

    hunter_state_raw = _load_hunter_state_file()
    # Filter hunter state to only repos enabled for obsidian
    hunter_state = {
        k: v for k, v in hunter_state_raw.items()
        if v.get("repo", "") in obsidian_repos
    }

    hunter_events_raw = _read_jsonl_since(HUNTER_DECISION_LOG, effective_since)
    hunter_events = [e for e in hunter_events_raw if e.get("repo", "") in obsidian_repos]

    predd_events_raw = _read_jsonl_since(DECISION_LOG, effective_since)
    predd_events = [e for e in predd_events_raw if e.get("repo", "") in obsidian_repos]

    observations = _build_observations(hunter_state, hunter_events, predd_events, effective_since)

    written = 0
    for obs in observations:
        path = _write_observation_note(obs, dry_run=dry_run)
        written += 1
        logger.debug("observe: wrote %s", path)

    if not dry_run:
        _save_last_observe(now)

    logger.info("observe: wrote %d observation notes", written)


# ---------------------------------------------------------------------------
# Analyze helpers
# ---------------------------------------------------------------------------


def _load_recent_observations(n_days: int) -> list[dict]:
    """
    Load observation notes from the last n_days days.
    Returns list of dicts with keys: path, date, content.
    """
    if not OBSIDIAN_OBSERVATIONS_DIR.exists():
        return []

    cutoff = datetime.now(timezone.utc)
    from datetime import timedelta
    cutoff_date = (cutoff - timedelta(days=n_days)).strftime("%Y-%m-%d")

    notes = []
    for f in sorted(OBSIDIAN_OBSERVATIONS_DIR.iterdir()):
        if not f.suffix == ".md":
            continue
        # Filename starts with YYYY-MM-DD
        date_str = f.name[:10]
        if date_str < cutoff_date:
            continue
        try:
            content = f.read_text()
        except OSError:
            continue
        notes.append({
            "path": str(f),
            "date": date_str,
            "name": f.stem,
            "content": content,
        })
    return notes


def _build_analyze_prompt(notes: list[dict], n_days: int) -> str:
    notes_text = "\n\n---\n\n".join(
        f"### {n['name']}\n\n{n['content']}" for n in notes
    )
    return f"""\
You are analyzing observations of an AI-powered code review and issue pipeline (predd + hunter).

Here are observation notes from the last {n_days} days:

{notes_text}

Identify:
1. Recurring patterns in what human reviewers request that the AI missed
2. Patterns in proposal quality issues (missing sections, wrong scope, vague tasks)
3. Patterns in implementation quality issues (missing tests, incomplete coverage)
4. Cases where hunter got stuck or failed — what caused it?
5. Any hunter/predd logic bugs or edge cases revealed by the observations

For each pattern:
- Describe it clearly
- Estimate how often it occurs (out of {len(notes)} observations)
- Suggest a concrete fix (skill improvement, prompt change, or code change)

If you identify a fix that should be implemented as a code/config change,
write it as a spec file using this format and place it in spec/changes/:
  - Filename: kebab-case description
  - Contents: follow the spec format in spec/changes/ (see existing examples)

Be direct. Prioritize by impact. Skip patterns with only 1 occurrence.
"""


def _write_analysis_note(response: str, notes: list[dict], n_days: int,
                         specs_written: int, dry_run: bool = False) -> str:
    """Write analysis note. Returns path."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    note_path = OBSIDIAN_ANALYSIS_DIR / f"{date_str}.md"

    now = _now_iso()
    # Date range
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc)
    start_date = (cutoff - timedelta(days=n_days)).strftime("%Y-%m-%d")
    end_date = cutoff.strftime("%Y-%m-%d")

    # Build observation links
    obs_links = "\n".join(f"- [[observations/{n['name']}]]" for n in notes)

    # Count patterns (rough: count ## headings in response that aren't "Specs Written"
    # or "Observations Analyzed")
    pattern_count = len(re.findall(r"^### \d+\.", response, re.MULTILINE))

    frontmatter = f"""\
---
type: analysis
period: {start_date} to {end_date}
observations_analyzed: {len(notes)}
patterns_found: {pattern_count}
specs_written: {specs_written}
analyzed_at: {now}
---
"""

    content = frontmatter + "\n" + response.strip()

    if specs_written > 0:
        # We'll append specs_written list after the fact in extract_and_write_specs
        pass

    if not dry_run:
        OBSIDIAN_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
        note_path.write_text(content)

    return str(note_path)


def _extract_and_write_specs(response: str, dry_run: bool = False) -> int:
    """
    Parse fenced code blocks labeled spec:filename.md from response.
    Write them to spec/changes/. Returns count written.
    """
    # Match ```spec:filename.md ... ```
    pattern = re.compile(
        r"```spec:([^\n`]+\.md)\n(.*?)```",
        re.DOTALL,
    )
    written = 0
    for m in pattern.finditer(response):
        filename = m.group(1).strip()
        content = m.group(2)
        # Sanitize filename — only allow safe characters, strip path separators and ..
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "", filename)
        # Collapse repeated dots (prevent path traversal like ..)
        safe_name = re.sub(r"\.{2,}", ".", safe_name)
        if not safe_name.endswith(".md"):
            safe_name += ".md"
        dest = SPEC_CHANGES_DIR / safe_name
        if not dry_run:
            SPEC_CHANGES_DIR.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
            logger.info("analyze: wrote spec %s", dest)
        else:
            logger.info("analyze (dry-run): would write spec %s", dest)
        written += 1
    return written


# ---------------------------------------------------------------------------
# run_analyze
# ---------------------------------------------------------------------------


def run_analyze(cfg: Config, days: int | None = None, dry_run: bool = False) -> None:
    OBSIDIAN_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)

    n_days = days if days is not None else cfg.analyze_days
    notes = _load_recent_observations(n_days)

    if not notes:
        logger.info("analyze: no observations found for last %d days", n_days)
        return

    prompt = _build_analyze_prompt(notes, n_days)

    # Use analyze_model — may differ from default model
    cfg_copy_model = cfg.model
    cfg.model = cfg.analyze_model
    try:
        response = _run_llm(cfg, prompt, Path.cwd())
    finally:
        cfg.model = cfg_copy_model

    specs_written = _extract_and_write_specs(response, dry_run=dry_run)
    _write_analysis_note(response, notes, n_days, specs_written, dry_run=dry_run)

    logger.info("analyze: analyzed %d notes, wrote %d specs", len(notes), specs_written)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def cli():
    """obsidian: observe patterns in logs/feedback, generate improvement specs."""
    pass


@cli.command()
@click.option("--once", is_flag=True, help="Run one observe+analyze cycle then exit.")
def start(once: bool):
    """Run the obsidian daemon — observe hourly, analyze daily."""
    setup_logging()
    cfg = load_config()

    if not once:
        acquire_pid_file()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    _stop.clear()

    logger.info("obsidian started (once=%s)", once)

    last_analyze_date = None

    try:
        while not _stop.is_set():
            # Always observe
            try:
                run_observe(cfg)
            except Exception as e:
                logger.error("observe failed: %s", e, exc_info=True)

            # Analyze once per day at analyze_hour
            today = datetime.now().date()
            current_hour = datetime.now().hour
            if last_analyze_date != today and current_hour >= cfg.analyze_hour:
                try:
                    run_analyze(cfg)
                    last_analyze_date = today
                except Exception as e:
                    logger.error("analyze failed: %s", e, exc_info=True)

            if once or _stop.wait(cfg.observe_interval):
                break

        logger.info("obsidian shutting down cleanly.")
    finally:
        if not once:
            release_pid_file()


@cli.command()
@click.option("--since", default=None, help="Only include activity since this ISO date.")
@click.option("--dry-run", is_flag=True, help="Print what would be written; don't write.")
def observe(since: str | None, dry_run: bool):
    """Read GitHub activity and write one observation note per active PR/issue."""
    setup_logging()
    cfg = load_config()
    run_observe(cfg, since=since, dry_run=dry_run)


@cli.command()
@click.option("--days", default=None, type=int,
              help="Days of observations to analyze (overrides config).")
@click.option("--dry-run", is_flag=True, help="Print analysis without writing files.")
def analyze(days: int | None, dry_run: bool):
    """Read observation notes and produce analysis note + spec files."""
    setup_logging()
    cfg = load_config()
    run_analyze(cfg, days=days, dry_run=dry_run)


if __name__ == "__main__":
    cli()
