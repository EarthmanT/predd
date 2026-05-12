"""Tests for hunter.py — targets >= 80% coverage."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call, ANY

import pytest

# ---------------------------------------------------------------------------
# Import hunter module (script, not package)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent))
import importlib.util

_spec = importlib.util.spec_from_file_location("hunter", Path(__file__).parent / "hunter.py")
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = """\
repos = ["owner/repo"]
worktree_base = "/tmp/hunter-test-worktrees"
github_user = "testuser"
"""


def _write_config(tmp_path: Path, content: str) -> Path:
    cfg_dir = tmp_path / ".config" / "predd"
    cfg_dir.mkdir(parents=True)
    cfg_file = cfg_dir / "config.toml"
    cfg_file.write_text(content)
    return cfg_file


def _make_cfg(tmp_path: Path, **overrides) -> h.Config:
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
    # Use predd's Config class (re-exported by hunter)
    return h.Config(data)


# ---------------------------------------------------------------------------
# TestHunterConfig
# ---------------------------------------------------------------------------

class TestHunterConfig:
    def test_new_fields_have_defaults(self, tmp_path, monkeypatch):
        cfg_file = _write_config(tmp_path, MINIMAL_CONFIG)
        monkeypatch.setattr(h._predd, "CONFIG_FILE", cfg_file)
        cfg = h.load_config()
        assert cfg.predd_only_repos == []
        assert cfg.hunter_only_repos == []
        assert cfg.branch_prefix == "usr/at"
        assert cfg.max_review_fix_loops == 1
        assert cfg.auto_review_draft is False

    def test_new_fields_loaded_from_config(self, tmp_path, monkeypatch):
        extra = (
            'predd_only_repos = ["owner/predd-only"]\n'
            'hunter_only_repos = ["owner/hunter-only"]\n'
            'branch_prefix = "feat"\n'
            'max_review_fix_loops = 3\n'
            'auto_review_draft = true\n'
        )
        cfg_file = _write_config(tmp_path, MINIMAL_CONFIG + extra)
        monkeypatch.setattr(h._predd, "CONFIG_FILE", cfg_file)
        cfg = h.load_config()
        assert cfg.predd_only_repos == ["owner/predd-only"]
        assert cfg.hunter_only_repos == ["owner/hunter-only"]
        assert cfg.branch_prefix == "feat"
        assert cfg.max_review_fix_loops == 3
        assert cfg.auto_review_draft is True

    def test_proposal_and_impl_skill_path_defaults(self, tmp_path, monkeypatch):
        cfg_file = _write_config(tmp_path, MINIMAL_CONFIG)
        monkeypatch.setattr(h._predd, "CONFIG_FILE", cfg_file)
        cfg = h.load_config()
        assert "proposal" in str(cfg.proposal_skill_path)
        assert "impl" in str(cfg.impl_skill_path)

    def test_to_dict_includes_new_fields(self, tmp_path, monkeypatch):
        cfg_file = _write_config(tmp_path, MINIMAL_CONFIG)
        monkeypatch.setattr(h._predd, "CONFIG_FILE", cfg_file)
        cfg = h.load_config()
        d = cfg.to_dict()
        assert "predd_only_repos" in d
        assert "hunter_only_repos" in d
        assert "branch_prefix" in d
        assert "max_review_fix_loops" in d
        assert "auto_review_draft" in d
        assert "proposal_skill_path" in d
        assert "impl_skill_path" in d

    def test_make_cfg_helper(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        assert cfg.repos == ["owner/repo"]
        assert cfg.github_user == "testuser"
        assert cfg.branch_prefix == "usr/at"


# ---------------------------------------------------------------------------
# TestIssueSlug
# ---------------------------------------------------------------------------

class TestIssueSlug:
    def test_basic_slug(self):
        assert h.issue_slug("Fix the bug") == "fix-the-bug"

    def test_special_chars_replaced(self):
        slug = h.issue_slug("Add OAuth2.0 support (v2)")
        assert slug == "add-oauth2-0-support-v2"

    def test_truncation(self):
        slug = h.issue_slug("A" * 100)
        assert len(slug) <= 30

    def test_no_leading_or_trailing_hyphens(self):
        slug = h.issue_slug("  ---  test  ---  ")
        assert not slug.startswith("-")
        assert not slug.endswith("-")

    def test_empty_string(self):
        slug = h.issue_slug("")
        assert slug == ""

    def test_numbers_preserved(self):
        slug = h.issue_slug("Issue 42")
        assert "42" in slug

    def test_max_len_param(self):
        slug = h.issue_slug("A very long title that should be truncated", max_len=10)
        assert len(slug) <= 10


# ---------------------------------------------------------------------------
# TestBranchNames
# ---------------------------------------------------------------------------

class TestBranchNames:
    def _cfg(self, tmp_path):
        return _make_cfg(tmp_path, branch_prefix="usr/at")

    def test_proposal_branch_format(self, tmp_path):
        cfg = self._cfg(tmp_path)
        branch = h.proposal_branch(cfg, 42, "Fix login bug")
        assert branch.startswith("usr/at/42-proposal-")
        assert "fix-login-bug" in branch

    def test_impl_branch_format(self, tmp_path):
        cfg = self._cfg(tmp_path)
        branch = h.impl_branch(cfg, 42, "Fix login bug")
        assert branch.startswith("usr/at/42-impl-")
        assert "fix-login-bug" in branch

    def test_custom_prefix(self, tmp_path):
        cfg = _make_cfg(tmp_path, branch_prefix="feat")
        assert h.proposal_branch(cfg, 1, "Test").startswith("feat/1-proposal-")
        assert h.impl_branch(cfg, 1, "Test").startswith("feat/1-impl-")

    def test_proposal_and_impl_differ(self, tmp_path):
        cfg = self._cfg(tmp_path)
        prop = h.proposal_branch(cfg, 7, "My feature")
        impl = h.impl_branch(cfg, 7, "My feature")
        assert prop != impl
        assert "proposal" in prop
        assert "impl" in impl


# ---------------------------------------------------------------------------
# TestTryClaimIssue
# ---------------------------------------------------------------------------

class TestTryClaimIssue:
    def _cfg(self, tmp_path):
        return _make_cfg(tmp_path)

    def test_successful_claim(self, tmp_path):
        cfg = self._cfg(tmp_path)
        issue_data = {
            "number": 1, "title": "Test", "author": {"login": "reporter"},
            "labels": [{"name": "testuser:in-progress"}],
            "body": "", "assignees": [],
        }
        with patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_view", return_value=issue_data), \
             patch("time.sleep"):
            result = h.try_claim_issue(cfg, "owner/repo", 1)
        assert result is True

    def test_label_absent_after_sleep_returns_false(self, tmp_path):
        cfg = self._cfg(tmp_path)
        # Re-read shows label NOT present (race lost)
        issue_data = {
            "number": 1, "title": "Test",
            "labels": [],  # label disappeared
            "body": "", "assignees": [],
        }
        with patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_view", return_value=issue_data), \
             patch("time.sleep"):
            result = h.try_claim_issue(cfg, "owner/repo", 1)
        assert result is False

    def test_competing_label_returns_false(self, tmp_path):
        cfg = self._cfg(tmp_path)
        # Another user's label appeared
        issue_data = {
            "number": 1, "title": "Test",
            "labels": [
                {"name": "testuser:in-progress"},
                {"name": "otheruser:in-progress"},  # competitor!
            ],
            "body": "", "assignees": [],
        }
        with patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_view", return_value=issue_data), \
             patch("time.sleep"):
            result = h.try_claim_issue(cfg, "owner/repo", 1)
        assert result is False

    def test_exception_returns_false(self, tmp_path):
        cfg = self._cfg(tmp_path)
        with patch.object(h, "gh_ensure_label_exists", side_effect=RuntimeError("network error")):
            result = h.try_claim_issue(cfg, "owner/repo", 1)
        assert result is False


# ---------------------------------------------------------------------------
# TestGhIssueHelpers
# ---------------------------------------------------------------------------

class TestGhIssueHelpers:
    def _fake_gh(self, stdout="[]"):
        fake = MagicMock()
        fake.stdout = stdout
        return fake

    def test_gh_list_assigned_issues(self):
        issues = [{"number": 1, "title": "Bug", "author": {"login": "user"}, "labels": [], "body": ""}]
        with patch("subprocess.run", return_value=self._fake_gh(json.dumps(issues))) as mock_run:
            result = h.gh_list_assigned_issues("owner/repo")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "gh"
        assert "issue" in args
        assert "list" in args
        assert "--assignee" in args
        assert "@me" in args
        assert "--repo" in args
        assert "owner/repo" in args
        assert result == issues

    def test_gh_issue_add_label(self):
        with patch("subprocess.run", return_value=self._fake_gh("")) as mock_run:
            h.gh_issue_add_label("owner/repo", 42, "testuser:in-progress")
        args = mock_run.call_args[0][0]
        assert "edit" in args
        assert "42" in args
        assert "--add-label" in args
        assert "testuser:in-progress" in args

    def test_gh_issue_remove_label(self):
        with patch("subprocess.run", return_value=self._fake_gh("")) as mock_run:
            h.gh_issue_remove_label("owner/repo", 42, "testuser:in-progress")
        args = mock_run.call_args[0][0]
        assert "--remove-label" in args

    def test_gh_ensure_label_exists_uses_force(self):
        with patch("subprocess.run", return_value=self._fake_gh("")) as mock_run:
            h.gh_ensure_label_exists("owner/repo", "myuser:in-progress")
        args = mock_run.call_args[0][0]
        assert "label" in args
        assert "create" in args
        assert "--force" in args

    def test_gh_ensure_label_exists_custom_color(self):
        with patch("subprocess.run", return_value=self._fake_gh("")) as mock_run:
            h.gh_ensure_label_exists("owner/repo", "myuser:in-progress", color="ff0000")
        args = mock_run.call_args[0][0]
        assert "--color" in args
        assert "ff0000" in args

    def test_gh_pr_is_merged_true(self):
        with patch("subprocess.run", return_value=self._fake_gh(json.dumps({"state": "MERGED"}))):
            assert h.gh_pr_is_merged("owner/repo", 5) is True

    def test_gh_pr_is_merged_false(self):
        with patch("subprocess.run", return_value=self._fake_gh(json.dumps({"state": "OPEN"}))):
            assert h.gh_pr_is_merged("owner/repo", 5) is False

    def test_gh_pr_is_draft_true(self):
        with patch("subprocess.run", return_value=self._fake_gh(json.dumps({"isDraft": True}))):
            assert h.gh_pr_is_draft("owner/repo", 5) is True

    def test_gh_pr_is_draft_false(self):
        with patch("subprocess.run", return_value=self._fake_gh(json.dumps({"isDraft": False}))):
            assert h.gh_pr_is_draft("owner/repo", 5) is False

    def test_gh_issue_reopen_and_reassign(self):
        calls = []
        def fake_run(args, **kwargs):
            calls.append(args)
            r = MagicMock()
            r.stdout = ""
            return r
        with patch("subprocess.run", side_effect=fake_run):
            h.gh_issue_reopen_and_reassign("owner/repo", 10, "reporter", "Please verify.")
        # Should have called reopen, edit, comment
        all_args = [" ".join(c) for c in calls]
        assert any("reopen" in a for a in all_args)
        assert any("edit" in a for a in all_args)
        assert any("comment" in a for a in all_args)

    def test_gh_list_prs_with_marker(self):
        prs = [
            {"number": 1, "body": "hunter:issue-42", "isDraft": True},
            {"number": 2, "body": "something else", "isDraft": False},
        ]
        with patch("subprocess.run", return_value=self._fake_gh(json.dumps(prs))):
            result = h.gh_list_prs_with_marker("owner/repo", "hunter:issue-42")
        assert len(result) == 1
        assert result[0]["number"] == 1

    def test_gh_pr_mark_ready(self):
        with patch("subprocess.run", return_value=self._fake_gh("")) as mock_run:
            h.gh_pr_mark_ready("owner/repo", 7)
        args = mock_run.call_args[0][0]
        assert "ready" in args
        assert "7" in args


# ---------------------------------------------------------------------------
# TestRunSkill
# ---------------------------------------------------------------------------

class TestRunSkill:
    def test_dispatches_to_claude(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("Do task $ARGUMENTS.")
        cfg = _make_cfg(tmp_path, backend="claude")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "_run_claude", return_value="done") as mock_claude:
            result = h.run_skill(cfg, skill, "42", worktree)
        mock_claude.assert_called_once()
        prompt_arg = mock_claude.call_args[0][1]
        assert "42" in prompt_arg
        assert result == "done"

    def test_dispatches_to_devin(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("Do task $ARGUMENTS.")
        cfg = _make_cfg(tmp_path, backend="devin")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "_run_devin_skill", return_value="done") as mock_devin:
            result = h.run_skill(cfg, skill, "99", worktree)
        mock_devin.assert_called_once()
        assert result == "done"

    def test_arguments_in_prompt(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("Workflow instructions here.")
        cfg = _make_cfg(tmp_path, backend="claude")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        captured = {}
        def fake_claude(cfg, prompt, wt):
            captured["prompt"] = prompt
            return ""
        with patch.object(h, "_run_claude", side_effect=fake_claude):
            h.run_skill(cfg, skill, "Issue #7: Fix the thing", worktree)
        assert "Issue #7: Fix the thing" in captured["prompt"]
        assert "Workflow instructions here." in captured["prompt"]

    def test_skill_not_found_raises(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        with pytest.raises(FileNotFoundError, match="Skill not found"):
            h.run_skill(cfg, tmp_path / "nonexistent.md", "42", worktree)

    def test_unknown_backend_raises(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("prompt $ARGUMENTS")
        cfg = _make_cfg(tmp_path, backend="gpt-banana")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        with pytest.raises(ValueError, match="Unknown backend"):
            h.run_skill(cfg, skill, "1", worktree)

    def test_devin_skill_places_file_in_skills_dir(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill_content = "Do the thing with $ARGUMENTS"
        skill.write_text(skill_content)
        cfg = _make_cfg(tmp_path, backend="devin")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        captured_cmd = {}
        def fake_run_proc(cmd, wt, env=None):
            captured_cmd["cmd"] = cmd
            return "ok"

        with patch.object(h, "_run_proc", side_effect=fake_run_proc):
            h._run_devin_skill(cfg, "prompt text", skill, worktree)

        placed = worktree / ".devin" / "skills" / "skill.md"
        assert placed.exists()
        assert placed.read_text() == skill_content

    def test_devin_skill_strips_env_vars(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("prompt")
        cfg = _make_cfg(tmp_path, backend="devin")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        captured = {}
        def fake_run_proc(cmd, wt, env=None):
            captured["env"] = env
            return "ok"

        with patch.dict(os.environ, {
            "CLAUDECODE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "HOME": "/home/testuser",
        }), patch.object(h, "_run_proc", side_effect=fake_run_proc):
            h._run_devin_skill(cfg, "prompt", skill, worktree)

        assert "CLAUDECODE" not in captured["env"]
        assert "HOME" in captured["env"]


# ---------------------------------------------------------------------------
# TestSelfReviewLoop
# ---------------------------------------------------------------------------

class TestSelfReviewLoop:
    def _cfg(self, tmp_path):
        cfg = _make_cfg(tmp_path, backend="claude", max_review_fix_loops=2)
        skill = tmp_path / "review-skill.md"
        skill.write_text("Review $ARGUMENTS")
        cfg.skill_path = skill
        return cfg

    def _entry(self, **overrides):
        base = {
            "issue_number": 5,
            "impl_pr": 10,
            "repo": "owner/repo",
            "title": "Test issue",
            "issue_author": "reporter",
            "status": "implementing",
            "review_loops_done": 0,
        }
        base.update(overrides)
        return base

    def test_approve_path_marks_ready(self, tmp_path):
        cfg = self._cfg(tmp_path)
        state = {}
        key = "owner/repo!5"
        entry = self._entry()
        worktree = tmp_path / "wt"
        worktree.mkdir()

        monkeypatch_state_file(tmp_path)

        with patch.object(h, "run_skill", return_value="## Verdict\nAPPROVE\n"), \
             patch.object(h, "gh_pr_mark_ready") as mock_ready, \
             patch.object(h, "save_hunter_state"), \
             patch.object(h, "HUNTER_STATE_FILE", tmp_path / "hunter-state.json"), \
             patch.object(h, "CONFIG_DIR", tmp_path):
            h.self_review_loop(cfg, state, "owner/repo", key, entry, worktree)

        mock_ready.assert_called_once_with("owner/repo", 10)
        assert state.get(key, {}).get("status") == "ready_for_review"

    def test_request_changes_triggers_fix(self, tmp_path):
        cfg = self._cfg(tmp_path)
        state = {}
        key = "owner/repo!5"
        entry = self._entry(review_loops_done=0)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        # Skill returns request-changes
        with patch.object(h, "run_skill", return_value="## Verdict\nREQUEST_CHANGES\n"), \
             patch.object(h, "_run_claude", return_value="fixed"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)), \
             patch.object(h, "save_hunter_state"), \
             patch.object(h, "CONFIG_DIR", tmp_path):
            h.self_review_loop(cfg, state, "owner/repo", key, entry, worktree)

        # Should increment loop counter and stay implementing
        assert state.get(key, {}).get("review_loops_done") == 1
        assert state.get(key, {}).get("status") == "implementing"

    def test_loop_exhaustion_flags_human(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.max_review_fix_loops = 1
        state = {}
        key = "owner/repo!5"
        entry = self._entry(review_loops_done=1)  # already at max
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "gh_issue_comment") as mock_comment, \
             patch.object(h, "gh_pr_mark_ready") as mock_ready, \
             patch.object(h, "save_hunter_state"), \
             patch.object(h, "CONFIG_DIR", tmp_path):
            h.self_review_loop(cfg, state, "owner/repo", key, entry, worktree)

        mock_comment.assert_called_once()
        comment_body = mock_comment.call_args[0][2]
        assert "exhausted" in comment_body.lower() or "Human review" in comment_body
        mock_ready.assert_called_once_with("owner/repo", 10)
        assert state.get(key, {}).get("status") == "ready_for_review"

    def test_skill_failure_sets_failed_status(self, tmp_path):
        cfg = self._cfg(tmp_path)
        state = {}
        key = "owner/repo!5"
        entry = self._entry()
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "run_skill", side_effect=RuntimeError("skill broke")), \
             patch.object(h, "save_hunter_state"), \
             patch.object(h, "CONFIG_DIR", tmp_path):
            h.self_review_loop(cfg, state, "owner/repo", key, entry, worktree)

        assert state.get(key, {}).get("status") == "failed"

    def test_devin_backend_fix_uses_devin(self, tmp_path):
        cfg = _make_cfg(tmp_path, backend="devin", max_review_fix_loops=2)
        skill = tmp_path / "review-skill.md"
        skill.write_text("Review $ARGUMENTS")
        cfg.skill_path = skill
        impl_skill = tmp_path / "impl-skill.md"
        impl_skill.write_text("Impl $ARGUMENTS")
        cfg.impl_skill_path = impl_skill

        state = {}
        key = "owner/repo!5"
        entry = self._entry(review_loops_done=0)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "run_skill", return_value="REQUEST_CHANGES"), \
             patch.object(h, "_run_devin_skill", return_value="fixed") as mock_devin, \
             patch("subprocess.run", return_value=MagicMock(returncode=0)), \
             patch.object(h, "save_hunter_state"), \
             patch.object(h, "CONFIG_DIR", tmp_path):
            h.self_review_loop(cfg, state, "owner/repo", key, entry, worktree)

        mock_devin.assert_called_once()


# ---------------------------------------------------------------------------
# TestCheckProposalMerged
# ---------------------------------------------------------------------------

def monkeypatch_state_file(tmp_path):
    """Set HUNTER_STATE_FILE to a temp path (module-level attribute)."""
    h.HUNTER_STATE_FILE = tmp_path / "hunter-state.json"
    h.CONFIG_DIR = tmp_path


class TestCheckProposalMerged:
    def _entry(self):
        return {
            "issue_number": 3,
            "proposal_pr": 20,
            "repo": "owner/repo",
            "title": "A feature",
            "issue_author": "reporter",
            "status": "proposal_open",
        }

    def test_not_merged_skips(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_pr_is_merged", return_value=False) as mock_merged, \
             patch.object(h, "gh_repo_default_branch") as mock_branch:
            h.check_proposal_merged(cfg, state, "owner/repo", "owner/repo!3", entry)

        mock_branch.assert_not_called()
        assert state == {}  # nothing changed

    def test_merged_starts_implementation(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)

        skill = tmp_path / "impl-skill.md"
        skill.write_text("Implement $ARGUMENTS")
        cfg.impl_skill_path = skill

        state = {}
        entry = self._entry()
        worktree = tmp_path / "impl-wt"
        worktree.mkdir()

        with patch.object(h, "gh_pr_is_merged", return_value=True), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", return_value=worktree), \
             patch.object(h, "run_skill", return_value="done"), \
             patch.object(h, "skill_has_commits", return_value=True), \
             patch.object(h, "gh_create_branch_and_pr", return_value=99), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "save_hunter_state"):
            h.check_proposal_merged(cfg, state, "owner/repo", "owner/repo!3", entry)

        assert state.get("owner/repo!3", {}).get("impl_pr") == 99
        assert state.get("owner/repo!3", {}).get("status") == "implementing"

    def test_gh_error_skips_gracefully(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_pr_is_merged", side_effect=RuntimeError("network")):
            h.check_proposal_merged(cfg, state, "owner/repo", "owner/repo!3", entry)

        assert state == {}

    def test_missing_proposal_pr_returns_early(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = {"issue_number": 3, "repo": "owner/repo", "title": "X", "status": "proposal_open"}

        with patch.object(h, "gh_pr_is_merged") as mock_merged:
            h.check_proposal_merged(cfg, state, "owner/repo", "owner/repo!3", entry)

        mock_merged.assert_not_called()


# ---------------------------------------------------------------------------
# TestCheckImplMerged
# ---------------------------------------------------------------------------

class TestCheckImplMerged:
    def _entry(self):
        return {
            "issue_number": 5,
            "impl_pr": 30,
            "repo": "owner/repo",
            "title": "A feature",
            "issue_author": "reporter",
            "status": "ready_for_review",
        }

    def test_not_merged_skips(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_pr_is_merged", return_value=False), \
             patch.object(h, "gh_issue_reopen_and_reassign") as mock_reopen:
            h.check_impl_merged(cfg, state, "owner/repo", "owner/repo!5", entry)

        mock_reopen.assert_not_called()

    def test_merged_reopens_and_reassigns(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_pr_is_merged", return_value=True), \
             patch.object(h, "gh_issue_reopen_and_reassign") as mock_reopen, \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "save_hunter_state"):
            h.check_impl_merged(cfg, state, "owner/repo", "owner/repo!5", entry)

        mock_reopen.assert_called_once()
        args = mock_reopen.call_args[0]
        assert args[0] == "owner/repo"
        assert args[1] == 5
        assert args[2] == "reporter"
        assert "30" in args[3]  # impl PR number mentioned in comment

    def test_merged_updates_status_to_awaiting_verification(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_pr_is_merged", return_value=True), \
             patch.object(h, "gh_issue_reopen_and_reassign"), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "save_hunter_state"):
            h.check_impl_merged(cfg, state, "owner/repo", "owner/repo!5", entry)

        assert state.get("owner/repo!5", {}).get("status") == "awaiting_verification"

    def test_missing_impl_pr_returns_early(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = {"issue_number": 5, "repo": "owner/repo", "status": "ready_for_review"}

        with patch.object(h, "gh_pr_is_merged") as mock_merged:
            h.check_impl_merged(cfg, state, "owner/repo", "owner/repo!5", entry)

        mock_merged.assert_not_called()

    def test_gh_error_skips_gracefully(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_pr_is_merged", side_effect=RuntimeError("network")):
            h.check_impl_merged(cfg, state, "owner/repo", "owner/repo!5", entry)

        assert state == {}


# ---------------------------------------------------------------------------
# TestDraftGating
# ---------------------------------------------------------------------------

class TestDraftGating:
    def _entry(self):
        return {
            "issue_number": 7,
            "impl_pr": 50,
            "repo": "owner/repo",
            "title": "Feature",
            "status": "implementing",
            "impl_worktree": "/tmp/fake-wt",
        }

    def test_draft_pr_skipped_when_auto_review_draft_false(self, tmp_path):
        cfg = _make_cfg(tmp_path, auto_review_draft=False)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_pr_is_draft", return_value=True), \
             patch.object(h, "self_review_loop") as mock_review:
            h.check_impl_ready_for_review(cfg, state, "owner/repo", "owner/repo!7", entry)

        mock_review.assert_not_called()

    def test_draft_pr_processed_when_auto_review_draft_true(self, tmp_path):
        cfg = _make_cfg(tmp_path, auto_review_draft=True)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_pr_is_draft", return_value=True), \
             patch.object(h, "self_review_loop") as mock_review, \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.exists", return_value=True):
            h.check_impl_ready_for_review(cfg, state, "owner/repo", "owner/repo!7", entry)

        mock_review.assert_called_once()

    def test_non_draft_pr_processed_regardless(self, tmp_path):
        cfg = _make_cfg(tmp_path, auto_review_draft=False)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_pr_is_draft", return_value=False), \
             patch.object(h, "self_review_loop") as mock_review, \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.exists", return_value=True):
            h.check_impl_ready_for_review(cfg, state, "owner/repo", "owner/repo!7", entry)

        mock_review.assert_called_once()

    def test_draft_check_error_skips(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_pr_is_draft", side_effect=RuntimeError("network")), \
             patch.object(h, "self_review_loop") as mock_review:
            h.check_impl_ready_for_review(cfg, state, "owner/repo", "owner/repo!7", entry)

        mock_review.assert_not_called()


# ---------------------------------------------------------------------------
# TestPollLoopFiltering
# ---------------------------------------------------------------------------

class TestPollLoopFiltering:
    def test_issue_has_hunter_labels_true(self):
        issue = {
            "number": 1,
            "labels": [{"name": "testuser:in-progress"}],
        }
        assert h._issue_has_hunter_labels(issue) is True

    def test_issue_has_hunter_labels_false(self):
        issue = {
            "number": 1,
            "labels": [{"name": "bug"}, {"name": "enhancement"}],
        }
        assert h._issue_has_hunter_labels(issue) is False

    def test_issue_with_proposal_open_label(self):
        issue = {
            "number": 1,
            "labels": [{"name": "user:proposal-open"}],
        }
        assert h._issue_has_hunter_labels(issue) is True

    def test_issue_with_implementing_label(self):
        issue = {
            "number": 1,
            "labels": [{"name": "user:implementing"}],
        }
        assert h._issue_has_hunter_labels(issue) is True

    def test_issue_with_awaiting_verification_label(self):
        issue = {
            "number": 1,
            "labels": [{"name": "user:awaiting-verification"}],
        }
        assert h._issue_has_hunter_labels(issue) is True

    def test_terminal_state_skipped_in_poll(self, tmp_path):
        """Issues in terminal states should not trigger process_issue."""
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)

        # Pre-populate state with a terminal state
        state = {
            "owner/repo!1": {
                "status": "awaiting_verification",
                "issue_number": 1,
                "repo": "owner/repo",
            }
        }

        issue = {
            "number": 1,
            "title": "Test",
            "author": {"login": "reporter"},
            "labels": [],
            "body": "",
        }

        with patch.object(h, "process_issue") as mock_process, \
             patch.object(h, "gh_list_assigned_issues", return_value=[issue]), \
             patch.object(h, "load_hunter_state", return_value=state), \
             patch.object(h, "save_hunter_state"):
            # Direct test: terminal state check
            key = f"owner/repo!{issue['number']}"
            entry = state.get(key, {})
            status = entry.get("status", "")
            if status in h.TERMINAL_STATES:
                pass  # would skip
            else:
                mock_process(cfg, state, "owner/repo", issue)

        mock_process.assert_not_called()

    def test_issues_with_hunter_labels_skipped(self, tmp_path):
        """Issues already claimed via labels should be skipped as new."""
        issue = {
            "number": 2,
            "title": "Already claimed",
            "author": {"login": "reporter"},
            "labels": [{"name": "testuser:in-progress"}],
            "body": "",
        }
        assert h._issue_has_hunter_labels(issue) is True


# ---------------------------------------------------------------------------
# TestHunterState
# ---------------------------------------------------------------------------

class TestHunterState:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", tmp_path / "hunter-state.json")
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        state = {"owner/repo!1": {"status": "proposal_open", "issue_number": 1}}
        h.save_hunter_state(state)
        loaded = h.load_hunter_state()
        assert loaded == state

    def test_load_empty_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", tmp_path / "nonexistent.json")
        assert h.load_hunter_state() == {}

    def test_load_recovers_from_corrupt_file(self, tmp_path, monkeypatch):
        sf = tmp_path / "hunter-state.json"
        sf.write_text("not valid json{{{")
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", sf)
        assert h.load_hunter_state() == {}

    def test_update_issue_state_creates_entry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", tmp_path / "hunter-state.json")
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        state = {}
        h.update_issue_state(state, "owner/repo!5", status="in_progress", issue_number=5)
        assert state["owner/repo!5"]["status"] == "in_progress"


# ---------------------------------------------------------------------------
# TestProcessIssue
# ---------------------------------------------------------------------------

class TestProcessIssue:
    def _issue(self):
        return {
            "number": 10,
            "title": "Fix the widget",
            "author": {"login": "reporter"},
            "labels": [],
            "body": "Some description",
        }

    def test_claim_failure_skips(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}

        with patch.object(h, "try_claim_issue", return_value=False), \
             patch.object(h, "gh_repo_default_branch") as mock_branch:
            h.process_issue(cfg, state, "owner/repo", self._issue())

        mock_branch.assert_not_called()
        # State should not have a new entry
        assert "owner/repo!10" not in state or state.get("owner/repo!10", {}).get("status") != "proposal_open"

    def test_successful_process_creates_proposal_pr(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        skill = tmp_path / "proposal-skill.md"
        skill.write_text("Write proposal for $ARGUMENTS")
        cfg.proposal_skill_path = skill

        state = {}
        worktree = tmp_path / "prop-wt"
        worktree.mkdir()

        with patch.object(h, "try_claim_issue", return_value=True), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", return_value=worktree), \
             patch.object(h, "run_skill", return_value="proposal text"), \
             patch.object(h, "skill_has_commits", return_value=True), \
             patch.object(h, "gh_create_branch_and_pr", return_value=55), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_remove_label"), \
             patch.object(h, "notify_sound"), \
             patch.object(h, "notify_toast"), \
             patch.object(h, "save_hunter_state"):
            h.process_issue(cfg, state, "owner/repo", self._issue())

        assert state.get("owner/repo!10", {}).get("proposal_pr") == 55
        assert state.get("owner/repo!10", {}).get("status") == "proposal_open"

    def test_no_commits_after_proposal_skill_sets_failed(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        skill = tmp_path / "proposal-skill.md"
        skill.write_text("Write proposal for $ARGUMENTS")
        cfg.proposal_skill_path = skill
        state = {}
        worktree = tmp_path / "prop-wt"
        worktree.mkdir()

        with patch.object(h, "try_claim_issue", return_value=True), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", return_value=worktree), \
             patch.object(h, "run_skill", return_value=""), \
             patch.object(h, "skill_has_commits", return_value=False), \
             patch.object(h, "gh_create_branch_and_pr") as mock_create_pr, \
             patch.object(h, "notify_sound"), \
             patch.object(h, "notify_toast"), \
             patch.object(h, "save_hunter_state"):
            h.process_issue(cfg, state, "owner/repo", self._issue())

        mock_create_pr.assert_not_called()
        assert state.get("owner/repo!10", {}).get("status") == "failed"

    def test_exception_sets_failed_status(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}

        with patch.object(h, "try_claim_issue", return_value=True), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", side_effect=RuntimeError("disk full")), \
             patch.object(h, "notify_sound"), \
             patch.object(h, "notify_toast"), \
             patch.object(h, "save_hunter_state"):
            h.process_issue(cfg, state, "owner/repo", self._issue())

        assert state.get("owner/repo!10", {}).get("status") == "failed"


# ---------------------------------------------------------------------------
# TestSetupLogging
# ---------------------------------------------------------------------------

class TestSetupLogging:
    def test_setup_logging_returns_logger(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(h, "HUNTER_LOG_FILE", tmp_path / "hunter-log.txt")
        result = h.setup_logging()
        assert result is not None

    def test_setup_logging_creates_config_dir(self, tmp_path, monkeypatch):
        log_dir = tmp_path / "new_dir"
        monkeypatch.setattr(h, "CONFIG_DIR", log_dir)
        monkeypatch.setattr(h, "HUNTER_LOG_FILE", log_dir / "hunter-log.txt")
        h.setup_logging()
        assert log_dir.exists()


# ---------------------------------------------------------------------------
# TestPidManagement
# ---------------------------------------------------------------------------

class TestPidManagement:
    def test_acquire_writes_pid(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "HUNTER_PID_FILE", tmp_path / "hunter-pid")
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        h.acquire_pid_file()
        assert (tmp_path / "hunter-pid").exists()
        pid_text = (tmp_path / "hunter-pid").read_text().strip()
        assert pid_text == str(os.getpid())

    def test_acquire_exits_if_running(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "hunter-pid"
        pid_file.write_text(str(os.getpid()))  # current PID = "running"
        monkeypatch.setattr(h, "HUNTER_PID_FILE", pid_file)
        with pytest.raises(SystemExit):
            h.acquire_pid_file()

    def test_acquire_overwrites_stale_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "hunter-pid"
        pid_file.write_text("999999999")  # non-existent PID
        monkeypatch.setattr(h, "HUNTER_PID_FILE", pid_file)
        with patch.object(h, "_pid_alive", return_value=False):
            h.acquire_pid_file()
        assert pid_file.read_text().strip() == str(os.getpid())

    def test_acquire_handles_invalid_pid_in_file(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "hunter-pid"
        pid_file.write_text("not-a-number")
        monkeypatch.setattr(h, "HUNTER_PID_FILE", pid_file)
        h.acquire_pid_file()  # should not raise
        assert pid_file.read_text().strip() == str(os.getpid())

    def test_release_removes_pid_file(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "hunter-pid"
        pid_file.write_text("123")
        monkeypatch.setattr(h, "HUNTER_PID_FILE", pid_file)
        h.release_pid_file()
        assert not pid_file.exists()

    def test_release_tolerates_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "HUNTER_PID_FILE", tmp_path / "no-pid")
        h.release_pid_file()  # should not raise

    def test_pid_alive_true(self):
        assert h._pid_alive(os.getpid()) is True

    def test_pid_alive_false(self):
        assert h._pid_alive(999999999) is False


# ---------------------------------------------------------------------------
# TestShutdown
# ---------------------------------------------------------------------------

class TestShutdown:
    def test_first_signal_sets_stop(self):
        h._stop.clear()
        h._active_proc_hunter = None
        h._shutdown(2, None)
        assert h._stop.is_set()
        h._stop.clear()  # cleanup

    def test_second_signal_force_quits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", tmp_path / "hunter-state.json")
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(h, "HUNTER_PID_FILE", tmp_path / "hunter-pid")
        h._stop.set()
        h._current_issue_key[:] = ["owner/repo!1"]
        state = {"owner/repo!1": {"status": "in_progress"}}
        sf = tmp_path / "hunter-state.json"
        sf.write_text(json.dumps(state))
        with pytest.raises(SystemExit):
            h._shutdown(2, None)
        h._stop.clear()
        h._current_issue_key.clear()

    def test_second_signal_with_active_proc(self, tmp_path, monkeypatch):
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", tmp_path / "hunter-state.json")
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(h, "HUNTER_PID_FILE", tmp_path / "hunter-pid")
        h._stop.set()
        h._current_issue_key[:] = []
        mock_proc = MagicMock()
        h._active_proc_hunter = mock_proc
        with pytest.raises(SystemExit):
            h._shutdown(2, None)
        mock_proc.terminate.assert_called_once()
        h._stop.clear()
        h._active_proc_hunter = None
        h._current_issue_key.clear()


# ---------------------------------------------------------------------------
# TestBuildIssueContext
# ---------------------------------------------------------------------------

class TestBuildIssueContext:
    def test_basic_output(self):
        result = h.build_issue_context(42, "Fix the bug", "Some description", {})
        assert "Issue #42: Fix the bug" in result
        assert "Description:" in result
        assert "Some description" in result

    def test_includes_optional_fields(self):
        entry = {"type": "Story", "epic": "DAP09A-100", "sprint": "Sprint-10", "capability": "99 auth"}
        result = h.build_issue_context(1, "Title", "Body", entry)
        assert "Type: Story" in result
        assert "Epic: DAP09A-100" in result
        assert "Sprint: Sprint-10" in result
        assert "Capability: 99 auth" in result

    def test_omits_missing_fields(self):
        result = h.build_issue_context(1, "Title", "Body", {"type": "Story"})
        assert "Epic" not in result
        assert "Sprint" not in result

    def test_fallback_when_no_body(self):
        result = h.build_issue_context(1, "Title", "", {})
        assert "(no description)" in result

    def test_arguments_substitution_compatible(self):
        # Verify $ARGUMENTS replacement works with multi-line context
        context = h.build_issue_context(1, "Title", "Body", {})
        skill = "Review this: $ARGUMENTS. Done."
        result = skill.replace("$ARGUMENTS", context)
        assert "Issue #1: Title" in result
        assert "$ARGUMENTS" not in result


# ---------------------------------------------------------------------------
# TestSkillHasCommits
# ---------------------------------------------------------------------------

class TestSkillHasCommits:
    def test_returns_true_when_unpushed_commits(self, tmp_path):
        results = [
            MagicMock(returncode=0, stdout=""),           # git status --porcelain (clean)
            MagicMock(returncode=0, stdout="abc123\n"),   # git log --not --remotes (has commits)
        ]
        with patch("subprocess.run", side_effect=results):
            assert h.skill_has_commits(tmp_path) is True

    def test_returns_true_when_uncommitted_changes(self, tmp_path):
        results = [
            MagicMock(returncode=0, stdout="M  file.py\n"),  # git status (dirty)
        ]
        with patch("subprocess.run", side_effect=results):
            assert h.skill_has_commits(tmp_path) is True

    def test_returns_false_when_clean_and_no_unpushed(self, tmp_path):
        results = [
            MagicMock(returncode=0, stdout=""),   # git status (clean)
            MagicMock(returncode=0, stdout=""),   # git log (no unpushed)
        ]
        with patch("subprocess.run", side_effect=results):
            assert h.skill_has_commits(tmp_path) is False

    def test_returns_false_on_git_error(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            assert h.skill_has_commits(tmp_path) is False


# ---------------------------------------------------------------------------
# TestGhCreateBranchAndPr
# ---------------------------------------------------------------------------

class TestGhCreateBranchAndPr:
    def test_returns_pr_number(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()

        def fake_run(args, **kwargs):
            r = MagicMock()
            r.stdout = "https://github.com/owner/repo/pull/42\n"
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=fake_run):
            pr_num = h.gh_create_branch_and_pr(
                "owner/repo", "main", "feat/1-test", "Test PR", "body", draft=True,
                worktree=worktree,
            )
        assert pr_num == 42

    def test_raises_if_pr_url_not_parseable(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()

        def fake_run(args, **kwargs):
            r = MagicMock()
            r.stdout = "not a url\n"
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(ValueError, match="Could not parse PR number"):
                h.gh_create_branch_and_pr(
                    "owner/repo", "main", "feat/1-test", "Test PR", "body",
                    worktree=worktree,
                )

    def test_no_draft_flag_when_draft_false(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        called_args = []

        def fake_run(args, **kwargs):
            called_args.append(list(args))
            r = MagicMock()
            r.stdout = "https://github.com/owner/repo/pull/5\n"
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=fake_run):
            h.gh_create_branch_and_pr(
                "owner/repo", "main", "feat/1-test", "Test PR", "body",
                draft=False, worktree=worktree,
            )
        gh_create_call = [a for a in called_args if "gh" in a]
        assert all("--draft" not in a for a in gh_create_call)


# ---------------------------------------------------------------------------
# TestSetupNewBranchWorktree
# ---------------------------------------------------------------------------

class TestSetupNewBranchWorktree:
    def test_clones_repo_when_no_local_repo(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        worktree_dir = tmp_path / "worktrees"
        worktree_dir.mkdir()
        cfg.worktree_base = worktree_dir
        branch = "usr/at/1-proposal-test"
        expected_wt = worktree_dir / f"owner-repo-{branch.replace('/', '-')}"

        with patch.object(h, "find_local_repo", return_value=None), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)):
            result = h.setup_new_branch_worktree(cfg, "owner/repo", branch, "main")
        assert result == expected_wt

    def test_uses_local_repo_when_found(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        worktree_dir = tmp_path / "worktrees"
        worktree_dir.mkdir()
        cfg.worktree_base = worktree_dir

        local_repo = tmp_path / "local-repo"
        local_repo.mkdir()
        branch = "usr/at/1-proposal-test"

        with patch.object(h, "find_local_repo", return_value=local_repo), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            h.setup_new_branch_worktree(cfg, "owner/repo", branch, "main")

        all_cmds = [" ".join(str(x) for x in c[0][0]) for c in mock_run.call_args_list]
        assert any("fetch" in cmd for cmd in all_cmds)
        assert any("worktree" in cmd for cmd in all_cmds)

    def test_removes_existing_wt_path_before_clone(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        worktree_dir = tmp_path / "worktrees"
        worktree_dir.mkdir()
        cfg.worktree_base = worktree_dir
        branch = "usr/at/1-test"
        # Pre-create the expected path
        wt_path = worktree_dir / f"owner-repo-{branch.replace('/', '-')}"
        wt_path.mkdir(parents=True)

        with patch.object(h, "find_local_repo", return_value=None), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)):
            result = h.setup_new_branch_worktree(cfg, "owner/repo", branch, "main")
        # Verify no error even though path existed


# ---------------------------------------------------------------------------
# TestGhIssueViewAndComment
# ---------------------------------------------------------------------------

class TestGhIssueViewAndComment:
    def test_gh_issue_view_calls_correctly(self):
        data = {"number": 1, "title": "Bug", "labels": [], "body": "desc", "assignees": []}
        fake = MagicMock()
        fake.stdout = json.dumps(data)
        with patch("subprocess.run", return_value=fake) as mock_run:
            result = h.gh_issue_view("owner/repo", 1)
        args = mock_run.call_args[0][0]
        assert "issue" in args
        assert "view" in args
        assert "1" in args
        assert result == data

    def test_gh_issue_comment_calls_correctly(self):
        fake = MagicMock()
        fake.stdout = ""
        with patch("subprocess.run", return_value=fake) as mock_run:
            h.gh_issue_comment("owner/repo", 5, "hello world")
        args = mock_run.call_args[0][0]
        assert "comment" in args
        assert "5" in args
        assert "--body" in args
        assert "hello world" in args

    def test_gh_repo_default_branch(self):
        fake = MagicMock()
        fake.stdout = json.dumps({"defaultBranchRef": {"name": "main"}})
        with patch("subprocess.run", return_value=fake):
            result = h.gh_repo_default_branch("owner/repo")
        assert result == "main"


# ---------------------------------------------------------------------------
# TestCheckImplReadyForReviewMissingPr
# ---------------------------------------------------------------------------

class TestCheckImplReadyForReviewMissingPr:
    def test_missing_impl_pr_returns_early(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = {}
        entry = {
            "issue_number": 7,
            "status": "implementing",
            "repo": "owner/repo",
        }
        with patch.object(h, "gh_pr_is_draft") as mock_draft:
            h.check_impl_ready_for_review(cfg, state, "owner/repo", "owner/repo!7", entry)
        mock_draft.assert_not_called()


# ---------------------------------------------------------------------------
# TestCheckProposalMergedExceptionInImpl
# ---------------------------------------------------------------------------

class TestCheckProposalMergedExceptionInImpl:
    def test_exception_in_impl_sets_failed(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        skill = tmp_path / "impl-skill.md"
        skill.write_text("Implement $ARGUMENTS")
        cfg.impl_skill_path = skill

        state = {}
        entry = {
            "issue_number": 3,
            "proposal_pr": 20,
            "repo": "owner/repo",
            "title": "A feature",
            "issue_author": "reporter",
            "status": "proposal_open",
        }

        with patch.object(h, "gh_pr_is_merged", return_value=True), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", side_effect=RuntimeError("clone failed")), \
             patch.object(h, "save_hunter_state"):
            h.check_proposal_merged(cfg, state, "owner/repo", "owner/repo!3", entry)

        assert state.get("owner/repo!3", {}).get("status") == "failed"


# ---------------------------------------------------------------------------
# TestCheckImplMergedError
# ---------------------------------------------------------------------------

class TestCheckImplMergedError:
    def test_reopen_failure_sets_failed(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = {
            "issue_number": 5,
            "impl_pr": 30,
            "repo": "owner/repo",
            "title": "A feature",
            "issue_author": "reporter",
            "status": "ready_for_review",
        }

        with patch.object(h, "gh_pr_is_merged", return_value=True), \
             patch.object(h, "gh_issue_reopen_and_reassign", side_effect=RuntimeError("network")), \
             patch.object(h, "save_hunter_state"):
            h.check_impl_merged(cfg, state, "owner/repo", "owner/repo!5", entry)

        assert state.get("owner/repo!5", {}).get("status") == "failed"


# ---------------------------------------------------------------------------
# TestCliCommands
# ---------------------------------------------------------------------------

class TestCliCommands:
    def test_list_empty_state(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", tmp_path / "hunter-state.json")
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        runner = CliRunner()
        result = runner.invoke(h.cli, ["list"])
        assert result.exit_code == 0
        assert "No tracked issues" in result.output

    def test_list_with_state(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        sf = tmp_path / "hunter-state.json"
        state = {"owner/repo!1": {"status": "proposal_open", "issue_number": 1}}
        sf.write_text(json.dumps(state))
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", sf)
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        runner = CliRunner()
        result = runner.invoke(h.cli, ["list"])
        assert result.exit_code == 0
        assert "proposal_open" in result.output

    def test_status_empty_state(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", tmp_path / "hunter-state.json")
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        runner = CliRunner()
        result = runner.invoke(h.cli, ["status"])
        assert result.exit_code == 0
        assert "No tracked issues" in result.output

    def test_status_with_state(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        sf = tmp_path / "hunter-state.json"
        state = {
            "owner/repo!1": {"status": "proposal_open"},
            "owner/repo!2": {"status": "implementing"},
            "owner/repo!3": {"status": "proposal_open"},
        }
        sf.write_text(json.dumps(state))
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", sf)
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        runner = CliRunner()
        result = runner.invoke(h.cli, ["status"])
        assert result.exit_code == 0
        assert "proposal_open: 2" in result.output
        assert "implementing: 1" in result.output

    def test_show_by_full_key(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        sf = tmp_path / "hunter-state.json"
        state = {"owner/repo!5": {"status": "implementing", "proposal_pr": 10, "impl_pr": 20}}
        sf.write_text(json.dumps(state))
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", sf)
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        runner = CliRunner()
        result = runner.invoke(h.cli, ["show", "owner/repo!5"])
        assert result.exit_code == 0
        assert "implementing" in result.output
        assert "Proposal PR: #10" in result.output
        assert "Impl PR: #20" in result.output

    def test_show_by_number(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        sf = tmp_path / "hunter-state.json"
        state = {"owner/repo!5": {"status": "proposal_open"}}
        sf.write_text(json.dumps(state))
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", sf)
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        runner = CliRunner()
        result = runner.invoke(h.cli, ["show", "5"])
        assert result.exit_code == 0
        assert "proposal_open" in result.output

    def test_show_not_found(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", tmp_path / "hunter-state.json")
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        runner = CliRunner()
        result = runner.invoke(h.cli, ["show", "999"])
        assert result.exit_code != 0

    def test_show_ambiguous(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        sf = tmp_path / "hunter-state.json"
        state = {
            "owner/repo-a!5": {"status": "implementing"},
            "owner/repo-b!5": {"status": "proposal_open"},
        }
        sf.write_text(json.dumps(state))
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", sf)
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        runner = CliRunner()
        result = runner.invoke(h.cli, ["show", "5"])
        assert result.exit_code != 0
        assert "Ambiguous" in result.output

    def test_show_invalid_arg(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", tmp_path / "hunter-state.json")
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        runner = CliRunner()
        result = runner.invoke(h.cli, ["show", "not-a-number"])
        assert result.exit_code != 0

    def test_show_key_not_in_state(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        sf = tmp_path / "hunter-state.json"
        sf.write_text("{}")
        monkeypatch.setattr(h, "HUNTER_STATE_FILE", sf)
        monkeypatch.setattr(h, "CONFIG_DIR", tmp_path)
        runner = CliRunner()
        result = runner.invoke(h.cli, ["show", "owner/repo!99"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# TestSelfReviewLoopExhaustionHandleErrors
# ---------------------------------------------------------------------------

class TestSelfReviewLoopExhaustionErrors:
    def test_exhaustion_comment_failure_still_marks_ready(self, tmp_path):
        cfg = _make_cfg(tmp_path, max_review_fix_loops=1)
        state = {}
        key = "owner/repo!5"
        entry = {
            "issue_number": 5,
            "impl_pr": 10,
            "review_loops_done": 1,
            "status": "implementing",
        }
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "gh_issue_comment", side_effect=RuntimeError("network")), \
             patch.object(h, "gh_pr_mark_ready") as mock_ready, \
             patch.object(h, "save_hunter_state"), \
             patch.object(h, "CONFIG_DIR", tmp_path):
            h.self_review_loop(cfg, state, "owner/repo", key, entry, worktree)

        mock_ready.assert_called_once()

    def test_exhaustion_mark_ready_failure_is_logged(self, tmp_path):
        cfg = _make_cfg(tmp_path, max_review_fix_loops=1)
        state = {}
        key = "owner/repo!5"
        entry = {
            "issue_number": 5,
            "impl_pr": 10,
            "review_loops_done": 1,
            "status": "implementing",
        }
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "gh_issue_comment"), \
             patch.object(h, "gh_pr_mark_ready", side_effect=RuntimeError("can't mark ready")), \
             patch.object(h, "save_hunter_state"), \
             patch.object(h, "CONFIG_DIR", tmp_path):
            # Should not raise
            h.self_review_loop(cfg, state, "owner/repo", key, entry, worktree)

    def test_fix_loop_subprocess_failure_sets_failed(self, tmp_path):
        cfg = _make_cfg(tmp_path, backend="claude", max_review_fix_loops=2)
        skill = tmp_path / "review-skill.md"
        skill.write_text("Review $ARGUMENTS")
        cfg.skill_path = skill
        state = {}
        key = "owner/repo!5"
        entry = {
            "issue_number": 5,
            "impl_pr": 10,
            "review_loops_done": 0,
            "status": "implementing",
        }
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "run_skill", return_value="REQUEST_CHANGES"), \
             patch.object(h, "_run_claude", side_effect=RuntimeError("backend failed")), \
             patch.object(h, "save_hunter_state"), \
             patch.object(h, "CONFIG_DIR", tmp_path):
            h.self_review_loop(cfg, state, "owner/repo", key, entry, worktree)

        assert state.get(key, {}).get("status") == "failed"

    def test_approve_mark_ready_failure_logged(self, tmp_path):
        cfg = _make_cfg(tmp_path, max_review_fix_loops=2)
        skill = tmp_path / "review-skill.md"
        skill.write_text("Review $ARGUMENTS")
        cfg.skill_path = skill
        state = {}
        key = "owner/repo!5"
        entry = {
            "issue_number": 5,
            "impl_pr": 10,
            "review_loops_done": 0,
            "status": "implementing",
        }
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "run_skill", return_value="APPROVE"), \
             patch.object(h, "gh_pr_mark_ready", side_effect=RuntimeError("network")), \
             patch.object(h, "save_hunter_state"), \
             patch.object(h, "CONFIG_DIR", tmp_path):
            # Should not raise
            h.self_review_loop(cfg, state, "owner/repo", key, entry, worktree)

        assert state.get(key, {}).get("status") == "ready_for_review"


# ---------------------------------------------------------------------------
# TestResumeAndRollback
# ---------------------------------------------------------------------------

def _base_entry(repo="owner/repo", issue_number=10, status="in_progress", **kwargs):
    return {
        "repo": repo,
        "issue_number": issue_number,
        "title": "Test issue",
        "status": status,
        "base_branch": "main",
        "resume_attempts": 0,
        **kwargs,
    }


class TestWorktreeHasCommitsSince:
    def test_returns_true_when_commits_present(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc1234 fix\n")
            result = h.worktree_has_commits_since(tmp_path, "main")
        assert result is True

    def test_returns_false_when_no_commits(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = h.worktree_has_commits_since(tmp_path, "main")
        assert result is False

    def test_falls_back_when_origin_ref_fails(self, tmp_path):
        results = [
            MagicMock(returncode=128, stdout=""),  # origin/main fails
            MagicMock(returncode=0, stdout="abc1234 fix\n"),  # fallback succeeds
        ]
        with patch("subprocess.run", side_effect=results):
            result = h.worktree_has_commits_since(tmp_path, "main")
        assert result is True


class TestRollbackIssue:
    def test_removes_labels_clears_state(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        key = "owner/repo!10"
        wt = tmp_path / "wt"
        wt.mkdir()
        state = {key: _base_entry(proposal_worktree=str(wt))}

        with patch.object(h, "gh_issue_remove_label") as mock_remove, \
             patch.object(h, "save_hunter_state"):
            h.rollback_issue(cfg, state, key, "test reason")

        assert key not in state
        assert mock_remove.call_count >= 1

    def test_deletes_worktree(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        key = "owner/repo!10"
        wt = tmp_path / "proposal-wt"
        wt.mkdir()
        state = {key: _base_entry(proposal_worktree=str(wt))}

        with patch.object(h, "gh_issue_remove_label"), \
             patch.object(h, "save_hunter_state"):
            h.rollback_issue(cfg, state, key, "test")

        assert not wt.exists()

    def test_tolerates_missing_worktree(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        key = "owner/repo!10"
        state = {key: _base_entry(proposal_worktree="/nonexistent/path")}

        with patch.object(h, "gh_issue_remove_label"), \
             patch.object(h, "save_hunter_state"):
            h.rollback_issue(cfg, state, key, "test")  # should not raise

    def test_tolerates_label_removal_failure(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        key = "owner/repo!10"
        state = {key: _base_entry()}

        with patch.object(h, "gh_issue_remove_label", side_effect=Exception("network")), \
             patch.object(h, "save_hunter_state"):
            h.rollback_issue(cfg, state, key, "test")  # should not raise


class TestResumeInFlightIssues:
    def test_skips_terminal_states(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {
            "owner/repo!1": _base_entry(status="awaiting_verification"),
            "owner/repo!2": _base_entry(issue_number=2, status="failed"),
        }
        with patch.object(h, "rollback_issue") as mock_rb:
            h.resume_in_flight_issues(cfg, state)
        mock_rb.assert_not_called()

    def test_rolls_back_exceeded_retries(self, tmp_path):
        cfg = _make_cfg(tmp_path, max_resume_retries=2)
        monkeypatch_state_file(tmp_path)
        key = "owner/repo!10"
        state = {key: _base_entry(status="in_progress", resume_attempts=3)}

        with patch.object(h, "rollback_issue") as mock_rb, \
             patch.object(h, "save_hunter_state"):
            h.resume_in_flight_issues(cfg, state)
        mock_rb.assert_called_once_with(cfg, state, key, ANY)

    def test_in_progress_with_worktree_and_commits_finds_pr(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        wt = tmp_path / "wt"
        wt.mkdir()
        key = "owner/repo!10"
        state = {key: _base_entry(status="in_progress", proposal_worktree=str(wt))}

        with patch.object(h, "worktree_has_commits_since", return_value=True), \
             patch.object(h, "gh_list_prs_with_marker", return_value=[{"number": 42}]), \
             patch.object(h, "save_hunter_state"):
            h.resume_in_flight_issues(cfg, state)

        assert state[key]["status"] == "proposal_open"
        assert state[key]["proposal_pr"] == 42

    def test_in_progress_no_commits_rolls_back(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        wt = tmp_path / "wt"
        wt.mkdir()
        key = "owner/repo!10"
        state = {key: _base_entry(status="in_progress", proposal_worktree=str(wt))}

        with patch.object(h, "worktree_has_commits_since", return_value=False), \
             patch.object(h, "rollback_issue") as mock_rb:
            h.resume_in_flight_issues(cfg, state)
        mock_rb.assert_called_once()

    def test_in_progress_no_worktree_rolls_back(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        key = "owner/repo!10"
        state = {key: _base_entry(status="in_progress")}

        with patch.object(h, "rollback_issue") as mock_rb:
            h.resume_in_flight_issues(cfg, state)
        mock_rb.assert_called_once()

    def test_implementing_no_impl_pr_with_worktree_finds_pr(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        wt = tmp_path / "wt"
        wt.mkdir()
        key = "owner/repo!10"
        state = {key: _base_entry(status="implementing", impl_worktree=str(wt))}

        with patch.object(h, "gh_list_prs_with_marker", return_value=[{"number": 77}]), \
             patch.object(h, "save_hunter_state"):
            h.resume_in_flight_issues(cfg, state)

        assert state[key]["impl_pr"] == 77

    def test_implementing_no_impl_pr_no_worktree_resets_to_proposal_open(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        key = "owner/repo!10"
        state = {key: _base_entry(status="implementing")}

        with patch.object(h, "save_hunter_state"):
            h.resume_in_flight_issues(cfg, state)

        assert state[key]["status"] == "proposal_open"

    def test_ready_for_review_no_impl_pr_rolls_back(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        key = "owner/repo!10"
        state = {key: _base_entry(status="ready_for_review")}

        with patch.object(h, "rollback_issue") as mock_rb:
            h.resume_in_flight_issues(cfg, state)
        mock_rb.assert_called_once()


class TestScanOrphanedLabels:
    def test_removes_label_from_issue_not_in_state(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}

        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = json.dumps([{"number": 42}])

        with patch.object(h, "gh_run", return_value=fake) as mock_gh, \
             patch.object(h, "gh_issue_remove_label") as mock_remove:
            h.scan_orphaned_labels(cfg, state, ["owner/repo"])

        mock_remove.assert_called_once_with("owner/repo", 42, f"{cfg.github_user}:in-progress")

    def test_skips_issue_already_in_state(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {"owner/repo!42": _base_entry(status="proposal_open")}

        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = json.dumps([{"number": 42}])

        with patch.object(h, "gh_run", return_value=fake), \
             patch.object(h, "gh_issue_remove_label") as mock_remove:
            h.scan_orphaned_labels(cfg, state, ["owner/repo"])

        mock_remove.assert_not_called()

    def test_tolerates_gh_failure(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}

        with patch.object(h, "gh_run", side_effect=Exception("network")):
            h.scan_orphaned_labels(cfg, state, ["owner/repo"])  # should not raise
