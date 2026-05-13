"""Tests for obsidian.py — targets >= 80% coverage."""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Import obsidian module (script, not package)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent))
import importlib.util

_spec = importlib.util.spec_from_file_location("obsidian", Path(__file__).parent / "obsidian.py")
ob = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ob)

# Access the predd module obsidian imported
_predd = ob._predd

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = """\
repos = ["owner/repo"]
worktree_base = "/tmp/obsidian-test-worktrees"
github_user = "testuser"
"""


def _write_config(tmp_path: Path, content: str) -> Path:
    cfg_dir = tmp_path / ".config" / "predd"
    cfg_dir.mkdir(parents=True)
    cfg_file = cfg_dir / "config.toml"
    cfg_file.write_text(content)
    return cfg_file


def _make_cfg(tmp_path: Path, **overrides):
    data = {
        "repos": ["owner/repo"],
        "worktree_base": str(tmp_path / "worktrees"),
        "github_user": "testuser",
        "backend": "claude",
        "model": "claude-opus-4-7",
        "skill_path": str(tmp_path / "review-skill.md"),
        "proposal_skill_path": str(tmp_path / "proposal-skill.md"),
        "impl_skill_path": str(tmp_path / "impl-skill.md"),
    }
    data.update(overrides)
    return ob.Config(data)


# ---------------------------------------------------------------------------
# TestObsidianConfig
# ---------------------------------------------------------------------------


class TestObsidianConfig:
    def test_new_fields_have_defaults(self, tmp_path, monkeypatch):
        cfg_file = _write_config(tmp_path, MINIMAL_CONFIG)
        monkeypatch.setattr(ob._predd, "CONFIG_FILE", cfg_file)
        cfg = ob.load_config()
        assert cfg.observe_interval == 3600
        assert cfg.analyze_hour == 8
        assert cfg.analyze_days == 7
        assert cfg.analyze_model == "claude-opus-4-7"

    def test_new_fields_loaded_from_config(self, tmp_path, monkeypatch):
        extra = (
            "observe_interval = 1800\n"
            "analyze_hour = 6\n"
            "analyze_days = 14\n"
            'analyze_model = "claude-sonnet-4"\n'
        )
        cfg_file = _write_config(tmp_path, MINIMAL_CONFIG + extra)
        monkeypatch.setattr(ob._predd, "CONFIG_FILE", cfg_file)
        cfg = ob.load_config()
        assert cfg.observe_interval == 1800
        assert cfg.analyze_hour == 6
        assert cfg.analyze_days == 14
        assert cfg.analyze_model == "claude-sonnet-4"

    def test_to_dict_includes_new_fields(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        d = cfg.to_dict()
        assert "observe_interval" in d
        assert "analyze_hour" in d
        assert "analyze_days" in d
        assert "analyze_model" in d

    def test_defaults_via_make_cfg(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        assert cfg.observe_interval == 3600
        assert cfg.analyze_hour == 8
        assert cfg.analyze_days == 7
        assert cfg.analyze_model == "claude-opus-4-7"


# ---------------------------------------------------------------------------
# TestLoadLastObserve
# ---------------------------------------------------------------------------


class TestLoadLastObserve:
    def test_returns_none_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_LAST_OBSERVE", tmp_path / ".last-observe")
        result = ob._load_last_observe()
        assert result is None

    def test_returns_timestamp_when_exists(self, tmp_path, monkeypatch):
        last = tmp_path / ".last-observe"
        ts = "2026-05-10T08:00:00Z"
        last.write_text(ts)
        monkeypatch.setattr(ob, "OBSIDIAN_LAST_OBSERVE", last)
        result = ob._load_last_observe()
        assert result == ts

    def test_returns_none_for_empty_file(self, tmp_path, monkeypatch):
        last = tmp_path / ".last-observe"
        last.write_text("   ")
        monkeypatch.setattr(ob, "OBSIDIAN_LAST_OBSERVE", last)
        result = ob._load_last_observe()
        assert result is None

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        last = tmp_path / ".last-observe"
        monkeypatch.setattr(ob, "OBSIDIAN_LAST_OBSERVE", last)
        monkeypatch.setattr(ob, "OBSIDIAN_DIR", tmp_path)
        ts = "2026-05-13T10:00:00Z"
        ob._save_last_observe(ts)
        assert ob._load_last_observe() == ts


# ---------------------------------------------------------------------------
# TestReadJsonlSince
# ---------------------------------------------------------------------------


class TestReadJsonlSince:
    def test_returns_empty_for_missing_file(self, tmp_path):
        result = ob._read_jsonl_since(tmp_path / "nonexistent.jsonl", None)
        assert result == []

    def test_returns_all_when_since_is_none(self, tmp_path):
        f = tmp_path / "test.jsonl"
        records = [
            {"ts": "2026-05-01T00:00:00Z", "event": "a"},
            {"ts": "2026-05-02T00:00:00Z", "event": "b"},
        ]
        f.write_text("\n".join(json.dumps(r) for r in records))
        result = ob._read_jsonl_since(f, None)
        assert len(result) == 2

    def test_filters_by_since(self, tmp_path):
        f = tmp_path / "test.jsonl"
        records = [
            {"ts": "2026-05-01T00:00:00Z", "event": "old"},
            {"ts": "2026-05-10T00:00:00Z", "event": "new"},
        ]
        f.write_text("\n".join(json.dumps(r) for r in records))
        result = ob._read_jsonl_since(f, "2026-05-05T00:00:00Z")
        assert len(result) == 1
        assert result[0]["event"] == "new"

    def test_skips_corrupt_lines(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text('{"ts": "2026-05-01T00:00:00Z", "event": "ok"}\nNOT_JSON\n{"ts": "2026-05-02T00:00:00Z", "event": "ok2"}\n')
        result = ob._read_jsonl_since(f, None)
        assert len(result) == 2
        assert result[0]["event"] == "ok"
        assert result[1]["event"] == "ok2"

    def test_handles_empty_lines(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text('\n{"ts": "2026-05-01T00:00:00Z", "event": "x"}\n\n')
        result = ob._read_jsonl_since(f, None)
        assert len(result) == 1

    def test_includes_equal_timestamp(self, tmp_path):
        f = tmp_path / "test.jsonl"
        ts = "2026-05-10T00:00:00Z"
        f.write_text(json.dumps({"ts": ts, "event": "x"}))
        result = ob._read_jsonl_since(f, ts)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# TestBuildObservations
# ---------------------------------------------------------------------------


class TestBuildObservations:
    def _make_state_entry(self, **overrides):
        entry = {
            "repo": "owner/repo",
            "issue_number": 10,
            "title": "Fix the thing",
            "status": "proposal_open",
            "proposal_pr": 11,
            "proposal_feedback": [
                {
                    "review_id": 1,
                    "ts": "2026-05-13T09:00:00Z",
                    "reviewer": "alice",
                    "type": "REQUEST_CHANGES",
                    "body": "Please fix error handling",
                    "inline_comments": [
                        {"path": "src/foo.py", "line": 42, "body": "Handle None here"},
                    ],
                }
            ],
        }
        entry.update(overrides)
        return entry

    def test_builds_pr_observation_from_proposal_feedback(self):
        state = {"owner/repo!10": self._make_state_entry()}
        obs = ob._build_observations(state, [], [], since=None)
        pr_obs = [o for o in obs if o["type"] == "pr"]
        assert len(pr_obs) >= 1
        assert pr_obs[0]["number"] == 11
        assert pr_obs[0]["label"] == "sdd-proposal"

    def test_builds_issue_observation_from_feedback(self):
        state = {"owner/repo!10": self._make_state_entry()}
        obs = ob._build_observations(state, [], [], since=None)
        issue_obs = [o for o in obs if o["type"] == "issue"]
        assert len(issue_obs) >= 1
        assert issue_obs[0]["number"] == 10
        assert issue_obs[0]["related_pr"] == 11

    def test_filters_feedback_by_since(self):
        state = {"owner/repo!10": self._make_state_entry()}
        # since is after the feedback timestamp
        obs = ob._build_observations(state, [], [], since="2026-05-14T00:00:00Z")
        # feedback was at 09:00 on the 13th — should be filtered out
        pr_obs = [o for o in obs if o["type"] == "pr"]
        assert len(pr_obs) == 0

    def test_includes_predd_events(self):
        predd_events = [
            {"ts": "2026-05-13T08:00:00Z", "event": "pr_review_posted",
             "repo": "owner/repo", "pr": 5, "verdict": "APPROVE"},
        ]
        obs = ob._build_observations({}, [], predd_events, since=None)
        pr_obs = [o for o in obs if o["type"] == "pr" and o["number"] == 5]
        assert len(pr_obs) == 1
        assert pr_obs[0]["reviews"][0]["state"] == "APPROVE"

    def test_no_duplicate_pr_from_predd_if_already_in_hunter_state(self):
        state = {"owner/repo!10": self._make_state_entry(proposal_pr=5)}
        predd_events = [
            {"ts": "2026-05-13T08:00:00Z", "event": "pr_review_posted",
             "repo": "owner/repo", "pr": 5, "verdict": "APPROVE"},
        ]
        obs = ob._build_observations(state, [], predd_events, since=None)
        pr_obs = [o for o in obs if o["type"] == "pr" and o["number"] == 5]
        assert len(pr_obs) == 1  # not duplicated

    def test_impl_pr_feedback_also_observed(self):
        entry = self._make_state_entry()
        entry["impl_pr"] = 20
        entry["impl_feedback"] = [
            {
                "review_id": 2,
                "ts": "2026-05-13T10:00:00Z",
                "reviewer": "bob",
                "type": "APPROVE",
                "body": "LGTM",
                "inline_comments": [],
            }
        ]
        state = {"owner/repo!10": entry}
        obs = ob._build_observations(state, [], [], since=None)
        impl_obs = [o for o in obs if o["type"] == "pr" and o["number"] == 20]
        assert len(impl_obs) == 1
        assert impl_obs[0]["label"] == "sdd-implementation"

    def test_empty_state_and_events(self):
        obs = ob._build_observations({}, [], [], since=None)
        assert obs == []


# ---------------------------------------------------------------------------
# TestWriteObservationNote
# ---------------------------------------------------------------------------


class TestWriteObservationNote:
    def _pr_obs(self):
        return {
            "type": "pr",
            "number": 42,
            "repo": "owner/repo",
            "title": "Proposal: Fix bug",
            "label": "sdd-proposal",
            "issue_number": 41,
            "reviews": [
                {
                    "state": "REQUEST_CHANGES",
                    "reviewer": "alice",
                    "ts": "2026-05-13T10:00:00Z",
                    "body": "Missing tests.",
                    "inline_comments": [
                        {"path": "src/foo.py", "line": 5, "body": "Handle edge case"},
                    ],
                }
            ],
            "comments": [
                {"reviewer": "bob", "ts": "2026-05-13T10:05:00Z", "body": "Also fix docs."}
            ],
            "observed_at": "2026-05-13T10:00:00Z",
        }

    def _issue_obs(self):
        return {
            "type": "issue",
            "number": 41,
            "repo": "owner/repo",
            "title": "Fix bug",
            "status": "proposal_open",
            "related_pr": 42,
            "feedback_summary": ["Missing tests.", "Handle edge case"],
            "observed_at": "2026-05-13T10:00:00Z",
        }

    def test_pr_note_frontmatter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path)
        path = ob._write_observation_note(self._pr_obs())
        content = Path(path).read_text()
        assert "type: pr-observation" in content
        assert "pr: 42" in content
        assert "issue: 41" in content
        assert "label: sdd-proposal" in content

    def test_pr_note_reviews_section(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path)
        path = ob._write_observation_note(self._pr_obs())
        content = Path(path).read_text()
        assert "## Reviews" in content
        assert "REQUEST_CHANGES — alice" in content
        assert "Missing tests." in content

    def test_pr_note_inline_comments(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path)
        path = ob._write_observation_note(self._pr_obs())
        content = Path(path).read_text()
        assert "**Inline comments:**" in content
        assert "src/foo.py:5" in content
        assert "Handle edge case" in content

    def test_pr_note_comments_section(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path)
        path = ob._write_observation_note(self._pr_obs())
        content = Path(path).read_text()
        assert "## Comments" in content
        assert "bob" in content
        assert "Also fix docs." in content

    def test_issue_note_frontmatter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path)
        path = ob._write_observation_note(self._issue_obs())
        content = Path(path).read_text()
        assert "type: issue-observation" in content
        assert "issue: 41" in content
        assert "status: proposal_open" in content

    def test_issue_note_current_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path)
        path = ob._write_observation_note(self._issue_obs())
        content = Path(path).read_text()
        assert "## Current State" in content
        assert "Related PR #42" in content

    def test_issue_note_feedback_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path)
        path = ob._write_observation_note(self._issue_obs())
        content = Path(path).read_text()
        assert "## Feedback Summary" in content
        assert "Missing tests." in content

    def test_issue_note_related_link(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path)
        path = ob._write_observation_note(self._issue_obs())
        content = Path(path).read_text()
        assert "## Related" in content
        assert "pr-42" in content

    def test_dry_run_does_not_write(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path)
        path_str = ob._write_observation_note(self._pr_obs(), dry_run=True)
        assert not Path(path_str).exists()

    def test_filename_format_pr(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path)
        path = ob._write_observation_note(self._pr_obs())
        filename = Path(path).name
        assert filename.endswith("-pr-42.md")
        assert len(filename.split("-")[:3]) == 3  # YYYY-MM-DD

    def test_filename_format_issue(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path)
        path = ob._write_observation_note(self._issue_obs())
        filename = Path(path).name
        assert filename.endswith("-issue-41.md")


# ---------------------------------------------------------------------------
# TestRunObserve
# ---------------------------------------------------------------------------


class TestRunObserve:
    def _minimal_hunter_state(self):
        return {
            "owner/repo!10": {
                "repo": "owner/repo",
                "issue_number": 10,
                "title": "Test Issue",
                "status": "proposal_open",
                "proposal_pr": 11,
                "proposal_feedback": [
                    {
                        "review_id": 1,
                        "ts": "2026-05-13T09:00:00Z",
                        "reviewer": "alice",
                        "type": "REQUEST_CHANGES",
                        "body": "Please fix",
                        "inline_comments": [],
                    }
                ],
            }
        }

    def test_writes_observation_notes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path / "observations")
        monkeypatch.setattr(ob, "OBSIDIAN_LAST_OBSERVE", tmp_path / ".last-observe")
        monkeypatch.setattr(ob, "OBSIDIAN_DIR", tmp_path)
        monkeypatch.setattr(ob, "HUNTER_DECISION_LOG", tmp_path / "hunter-decisions.jsonl")
        monkeypatch.setattr(ob, "DECISION_LOG", tmp_path / "decisions.jsonl")
        monkeypatch.setattr(ob, "_load_hunter_state_file", lambda: self._minimal_hunter_state())

        cfg = _make_cfg(tmp_path)
        ob.run_observe(cfg)

        files = list((tmp_path / "observations").glob("*.md"))
        assert len(files) >= 1

    def test_updates_last_observe_timestamp(self, tmp_path, monkeypatch):
        last_file = tmp_path / ".last-observe"
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path / "observations")
        monkeypatch.setattr(ob, "OBSIDIAN_LAST_OBSERVE", last_file)
        monkeypatch.setattr(ob, "OBSIDIAN_DIR", tmp_path)
        monkeypatch.setattr(ob, "HUNTER_DECISION_LOG", tmp_path / "hunter-decisions.jsonl")
        monkeypatch.setattr(ob, "DECISION_LOG", tmp_path / "decisions.jsonl")
        monkeypatch.setattr(ob, "_load_hunter_state_file", lambda: {})

        cfg = _make_cfg(tmp_path)
        ob.run_observe(cfg)

        assert last_file.exists()
        ts = last_file.read_text().strip()
        assert ts.endswith("Z")

    def test_dry_run_does_not_update_timestamp(self, tmp_path, monkeypatch):
        last_file = tmp_path / ".last-observe"
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path / "observations")
        monkeypatch.setattr(ob, "OBSIDIAN_LAST_OBSERVE", last_file)
        monkeypatch.setattr(ob, "OBSIDIAN_DIR", tmp_path)
        monkeypatch.setattr(ob, "HUNTER_DECISION_LOG", tmp_path / "hunter-decisions.jsonl")
        monkeypatch.setattr(ob, "DECISION_LOG", tmp_path / "decisions.jsonl")
        monkeypatch.setattr(ob, "_load_hunter_state_file", lambda: {})

        cfg = _make_cfg(tmp_path)
        ob.run_observe(cfg, dry_run=True)

        assert not last_file.exists()

    def test_respects_since_parameter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path / "observations")
        monkeypatch.setattr(ob, "OBSIDIAN_LAST_OBSERVE", tmp_path / ".last-observe")
        monkeypatch.setattr(ob, "OBSIDIAN_DIR", tmp_path)
        monkeypatch.setattr(ob, "HUNTER_DECISION_LOG", tmp_path / "hunter-decisions.jsonl")
        monkeypatch.setattr(ob, "DECISION_LOG", tmp_path / "decisions.jsonl")
        monkeypatch.setattr(ob, "_load_hunter_state_file", lambda: self._minimal_hunter_state())

        cfg = _make_cfg(tmp_path)
        # Set since to future — feedback from the 13th should be filtered out
        ob.run_observe(cfg, since="2026-05-14T00:00:00Z")

        files = list((tmp_path / "observations").glob("*.md"))
        assert len(files) == 0

    def test_no_crash_on_empty_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", tmp_path / "observations")
        monkeypatch.setattr(ob, "OBSIDIAN_LAST_OBSERVE", tmp_path / ".last-observe")
        monkeypatch.setattr(ob, "OBSIDIAN_DIR", tmp_path)
        monkeypatch.setattr(ob, "HUNTER_DECISION_LOG", tmp_path / "hunter-decisions.jsonl")
        monkeypatch.setattr(ob, "DECISION_LOG", tmp_path / "decisions.jsonl")
        monkeypatch.setattr(ob, "_load_hunter_state_file", lambda: {})

        cfg = _make_cfg(tmp_path)
        ob.run_observe(cfg)  # should not raise


# ---------------------------------------------------------------------------
# TestAnalyzePrompt
# ---------------------------------------------------------------------------


class TestAnalyzePrompt:
    def _make_notes(self, n=3):
        return [
            {"path": f"/tmp/obs-{i}.md", "date": "2026-05-13",
             "name": f"2026-05-13-pr-{i}", "content": f"## Review {i}\nFeedback {i}"}
            for i in range(n)
        ]

    def test_prompt_contains_observation_notes(self):
        notes = self._make_notes(2)
        prompt = ob._build_analyze_prompt(notes, 7)
        assert "Feedback 0" in prompt
        assert "Feedback 1" in prompt

    def test_prompt_contains_day_count(self):
        notes = self._make_notes(2)
        prompt = ob._build_analyze_prompt(notes, 14)
        assert "14" in prompt

    def test_prompt_contains_observation_count(self):
        notes = self._make_notes(5)
        prompt = ob._build_analyze_prompt(notes, 7)
        # Observation count (5) appears somewhere in prompt for frequency estimates
        assert "5" in prompt

    def test_prompt_mentions_predd_and_hunter(self):
        notes = self._make_notes(1)
        prompt = ob._build_analyze_prompt(notes, 7)
        assert "predd" in prompt.lower()
        assert "hunter" in prompt.lower()

    def test_prompt_mentions_spec(self):
        notes = self._make_notes(1)
        prompt = ob._build_analyze_prompt(notes, 7)
        assert "spec" in prompt.lower()


# ---------------------------------------------------------------------------
# TestExtractAndWriteSpecs
# ---------------------------------------------------------------------------


class TestExtractAndWriteSpecs:
    RESPONSE_WITH_SPECS = """\
Here is my analysis.

```spec:improve-error-handling.md
# Improve Error Handling

## What
Better error messages.
```

```spec:add-tests.md
# Add Tests

## What
More coverage.
```
"""

    RESPONSE_NO_SPECS = """\
No patterns found worth implementing as specs.
"""

    def test_extracts_and_writes_spec_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "SPEC_CHANGES_DIR", tmp_path / "spec" / "changes")
        count = ob._extract_and_write_specs(self.RESPONSE_WITH_SPECS)
        assert count == 2
        assert (tmp_path / "spec" / "changes" / "improve-error-handling.md").exists()
        assert (tmp_path / "spec" / "changes" / "add-tests.md").exists()

    def test_spec_content_is_correct(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "SPEC_CHANGES_DIR", tmp_path / "spec" / "changes")
        ob._extract_and_write_specs(self.RESPONSE_WITH_SPECS)
        content = (tmp_path / "spec" / "changes" / "improve-error-handling.md").read_text()
        assert "Better error messages." in content

    def test_returns_zero_when_no_specs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "SPEC_CHANGES_DIR", tmp_path / "spec" / "changes")
        count = ob._extract_and_write_specs(self.RESPONSE_NO_SPECS)
        assert count == 0

    def test_dry_run_does_not_write(self, tmp_path, monkeypatch):
        spec_dir = tmp_path / "spec" / "changes"
        monkeypatch.setattr(ob, "SPEC_CHANGES_DIR", spec_dir)
        count = ob._extract_and_write_specs(self.RESPONSE_WITH_SPECS, dry_run=True)
        assert count == 2
        assert not spec_dir.exists() or not list(spec_dir.glob("*.md"))

    def test_sanitizes_filename(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ob, "SPEC_CHANGES_DIR", tmp_path / "spec" / "changes")
        response = "```spec:bad/chars/../file.md\ncontent\n```"
        ob._extract_and_write_specs(response)
        # Should sanitize — only alphanumeric + ._- allowed
        files = list((tmp_path / "spec" / "changes").glob("*.md")) if (tmp_path / "spec" / "changes").exists() else []
        for f in files:
            assert "/" not in f.name
            assert ".." not in f.name


# ---------------------------------------------------------------------------
# TestRunAnalyze
# ---------------------------------------------------------------------------


class TestRunAnalyze:
    def _make_observation_files(self, obs_dir: Path, count: int = 3):
        obs_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for i in range(count):
            f = obs_dir / f"{today}-pr-{i+100}.md"
            f.write_text(f"---\ntype: pr-observation\npr: {i+100}\n---\n\n## Review\nFeedback {i}\n")

    def test_calls_run_claude(self, tmp_path, monkeypatch):
        obs_dir = tmp_path / "observations"
        self._make_observation_files(obs_dir)
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", obs_dir)
        monkeypatch.setattr(ob, "OBSIDIAN_ANALYSIS_DIR", tmp_path / "analysis")
        monkeypatch.setattr(ob, "SPEC_CHANGES_DIR", tmp_path / "spec" / "changes")

        called_with = {}

        def fake_run_claude(cfg, prompt, worktree):
            called_with["prompt"] = prompt
            called_with["model"] = cfg.model
            return "## Analysis\n### 1. Pattern found (3/3 observations)\nDetail."

        monkeypatch.setattr(ob, "_run_claude", fake_run_claude)
        cfg = _make_cfg(tmp_path)
        ob.run_analyze(cfg)

        assert "prompt" in called_with
        assert called_with["model"] == "claude-opus-4-7"  # analyze_model default

    def test_restores_model_after_analyze(self, tmp_path, monkeypatch):
        obs_dir = tmp_path / "observations"
        self._make_observation_files(obs_dir)
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", obs_dir)
        monkeypatch.setattr(ob, "OBSIDIAN_ANALYSIS_DIR", tmp_path / "analysis")
        monkeypatch.setattr(ob, "SPEC_CHANGES_DIR", tmp_path / "spec" / "changes")
        monkeypatch.setattr(ob, "_run_claude", lambda cfg, p, wt: "analysis output")

        cfg = _make_cfg(tmp_path, model="swe-1.6", analyze_model="claude-opus-4-7")
        ob.run_analyze(cfg)
        assert cfg.model == "swe-1.6"

    def test_restores_model_even_on_exception(self, tmp_path, monkeypatch):
        obs_dir = tmp_path / "observations"
        self._make_observation_files(obs_dir)
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", obs_dir)
        monkeypatch.setattr(ob, "OBSIDIAN_ANALYSIS_DIR", tmp_path / "analysis")
        monkeypatch.setattr(ob, "SPEC_CHANGES_DIR", tmp_path / "spec" / "changes")
        monkeypatch.setattr(ob, "_run_claude", lambda cfg, p, wt: (_ for _ in ()).throw(RuntimeError("boom")))

        cfg = _make_cfg(tmp_path, model="swe-1.6")
        with pytest.raises(RuntimeError):
            ob.run_analyze(cfg)
        assert cfg.model == "swe-1.6"

    def test_writes_analysis_note(self, tmp_path, monkeypatch):
        obs_dir = tmp_path / "observations"
        self._make_observation_files(obs_dir)
        analysis_dir = tmp_path / "analysis"
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", obs_dir)
        monkeypatch.setattr(ob, "OBSIDIAN_ANALYSIS_DIR", analysis_dir)
        monkeypatch.setattr(ob, "SPEC_CHANGES_DIR", tmp_path / "spec" / "changes")
        monkeypatch.setattr(ob, "_run_claude", lambda cfg, p, wt: "## Patterns\n### 1. Something (2/3)\nDetail.")

        cfg = _make_cfg(tmp_path)
        ob.run_analyze(cfg)

        files = list(analysis_dir.glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "type: analysis" in content

    def test_dry_run_skips_writes(self, tmp_path, monkeypatch):
        obs_dir = tmp_path / "observations"
        self._make_observation_files(obs_dir)
        analysis_dir = tmp_path / "analysis"
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", obs_dir)
        monkeypatch.setattr(ob, "OBSIDIAN_ANALYSIS_DIR", analysis_dir)
        monkeypatch.setattr(ob, "SPEC_CHANGES_DIR", tmp_path / "spec" / "changes")
        monkeypatch.setattr(ob, "_run_claude",
                            lambda cfg, p, wt: "```spec:test-spec.md\ncontent\n```")

        cfg = _make_cfg(tmp_path)
        ob.run_analyze(cfg, dry_run=True)

        assert not analysis_dir.exists() or not list(analysis_dir.glob("*.md"))
        spec_dir = tmp_path / "spec" / "changes"
        assert not spec_dir.exists() or not list(spec_dir.glob("*.md"))

    def test_skips_when_no_observations(self, tmp_path, monkeypatch):
        obs_dir = tmp_path / "observations"
        obs_dir.mkdir(parents=True)
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", obs_dir)
        monkeypatch.setattr(ob, "OBSIDIAN_ANALYSIS_DIR", tmp_path / "analysis")

        run_claude_called = []
        monkeypatch.setattr(ob, "_run_claude", lambda *a: run_claude_called.append(True) or "")

        cfg = _make_cfg(tmp_path)
        ob.run_analyze(cfg)

        assert not run_claude_called

    def test_uses_days_override(self, tmp_path, monkeypatch):
        obs_dir = tmp_path / "observations"
        self._make_observation_files(obs_dir)
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", obs_dir)
        monkeypatch.setattr(ob, "OBSIDIAN_ANALYSIS_DIR", tmp_path / "analysis")
        monkeypatch.setattr(ob, "SPEC_CHANGES_DIR", tmp_path / "spec" / "changes")

        prompts_seen = []
        monkeypatch.setattr(ob, "_run_claude", lambda cfg, p, wt: prompts_seen.append(p) or "output")

        cfg = _make_cfg(tmp_path)
        ob.run_analyze(cfg, days=3)

        assert prompts_seen
        assert "3" in prompts_seen[0]

    def test_extracts_specs_from_response(self, tmp_path, monkeypatch):
        obs_dir = tmp_path / "observations"
        self._make_observation_files(obs_dir)
        spec_dir = tmp_path / "spec" / "changes"
        monkeypatch.setattr(ob, "OBSIDIAN_OBSERVATIONS_DIR", obs_dir)
        monkeypatch.setattr(ob, "OBSIDIAN_ANALYSIS_DIR", tmp_path / "analysis")
        monkeypatch.setattr(ob, "SPEC_CHANGES_DIR", spec_dir)
        monkeypatch.setattr(ob, "_run_claude",
                            lambda cfg, p, wt: "```spec:new-feature.md\n# New Feature\n```")

        cfg = _make_cfg(tmp_path)
        ob.run_analyze(cfg)

        assert (spec_dir / "new-feature.md").exists()
