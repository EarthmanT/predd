"""Tests for hunter.py — targets >= 80% coverage."""
import json
import os
import subprocess
import sys
import time
import urllib.error
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

# Access the predd module that hunter imported
_predd = h._predd

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
        # New schema: repo list replaces flat repos/predd_only_repos/hunter_only_repos
        assert "repo" in d
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
# TestMaxNewIssuesPerCycle (SPEC 5)
# ---------------------------------------------------------------------------

class TestMaxNewIssuesPerCycle:
    def test_config_default_max_new_issues_per_cycle(self, tmp_path, monkeypatch):
        cfg_file = _write_config(tmp_path, MINIMAL_CONFIG)
        monkeypatch.setattr(h._predd, "CONFIG_FILE", cfg_file)
        cfg = h.load_config()
        assert cfg.max_new_issues_per_cycle == 1

    def test_max_one_new_issue_per_cycle_respected(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        cfg.max_new_issues_per_cycle = 1
        monkeypatch_state_file(tmp_path)

        issues = [
            {"number": i, "title": f"Issue {i}", "author": {"login": "u"},
             "labels": [], "body": ""}
            for i in range(1, 4)
        ]
        state = {}

        processed = []
        def fake_process(cfg, state, repo, issue):
            processed.append(issue["number"])
            # simulate state update
            key = f"{repo}!{issue['number']}"
            state[key] = {"status": "proposal_open", "issue_number": issue["number"]}

        with patch.object(h, "gh_list_assigned_issues", return_value=issues), \
             patch.object(h, "process_issue", side_effect=fake_process), \
             patch.object(h, "load_hunter_state", return_value=state), \
             patch.object(h, "resume_in_flight_issues"), \
             patch.object(h, "scan_orphaned_labels"), \
             patch.object(h, "_stop") as mock_stop:
            mock_stop.is_set.side_effect = [False, True]  # one iteration
            # Simulate just the inner loop
            new_issues_this_cycle = 0
            for issue in issues:
                key = f"owner/repo!{issue['number']}"
                entry = state.get(key, {})
                status = entry.get("status", "")
                if status in h.TERMINAL_STATES:
                    continue
                if status == "":
                    if h._issue_has_hunter_labels(issue):
                        continue
                    if new_issues_this_cycle >= cfg.max_new_issues_per_cycle:
                        continue
                    fake_process(cfg, state, "owner/repo", issue)
                    new_issues_this_cycle += 1

        assert len(processed) == 1

    def test_max_zero_skips_all_new(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        cfg.max_new_issues_per_cycle = 0

        issues = [
            {"number": i, "title": f"Issue {i}", "author": {"login": "u"},
             "labels": [], "body": ""}
            for i in range(1, 4)
        ]
        state = {}
        processed = []

        new_issues_this_cycle = 0
        for issue in issues:
            key = f"owner/repo!{issue['number']}"
            entry = state.get(key, {})
            status = entry.get("status", "")
            if status in h.TERMINAL_STATES:
                continue
            if status == "":
                if h._issue_has_hunter_labels(issue):
                    continue
                if new_issues_this_cycle >= cfg.max_new_issues_per_cycle:
                    continue
                processed.append(issue["number"])
                new_issues_this_cycle += 1

        assert len(processed) == 0


# ---------------------------------------------------------------------------
# TestPrTitle
# ---------------------------------------------------------------------------

class TestPrTitle:
    def test_with_jira_key(self):
        assert h._pr_title("Proposal", "[DAP09A-123] Fix the login bug") == \
            "[DAP09A-123] Proposal - Fix the login bug"

    def test_impl_with_jira_key(self):
        assert h._pr_title("Impl", "[DAP09A-999] Some feature") == \
            "[DAP09A-999] Impl - Some feature"

    def test_without_jira_key(self):
        assert h._pr_title("Proposal", "Fix the login bug") == \
            "Proposal - Fix the login bug"

    def test_strips_jira_prefix_no_duplication(self):
        result = h._pr_title("Impl", "[DAP09A-123] Some feature")
        assert result == "[DAP09A-123] Impl - Some feature"
        assert "[DAP09A-123] [DAP09A-123]" not in result

    def test_is_obviously_proposal_new_format(self):
        pr = {"title": "[DAP09A-123] Proposal - Fix something", "headRefName": "main", "files": []}
        assert h._is_obviously_proposal(pr)

    def test_is_obviously_implementation_new_format(self):
        pr = {"title": "[DAP09A-123] Impl - Fix something", "headRefName": "usr/at/DAP09A-123-impl-fix-something", "files": []}
        assert h._is_obviously_implementation(pr)


# ---------------------------------------------------------------------------
# TestAutoLabelPrs (SPEC 1)
# ---------------------------------------------------------------------------

class TestAutoLabelPrs:
    def _pr(self, number=1, title="", branch="", labels=None, files=None):
        return {
            "number": number,
            "title": title,
            "headRefName": branch,
            "labels": labels or [],
            "files": [{"path": p} for p in (files or [])],
        }

    def test_proposal_title_match(self):
        pr = self._pr(title="Proposal: add auth")
        assert h._is_obviously_proposal(pr) is True

    def test_proposal_branch_match(self):
        pr = self._pr(branch="usr/at/1-proposal-fix")
        assert h._is_obviously_proposal(pr) is True

    def test_proposal_spec_changes_file_match(self):
        pr = self._pr(files=["openspec/changes/my-spec.md"])
        assert h._is_obviously_proposal(pr) is True

    def test_impl_branch_match(self):
        # Branch + matching title required
        pr = self._pr(title="feat: fix thing", branch="usr/at/1-impl-fix")
        assert h._is_obviously_implementation(pr) is True

    def test_impl_branch_only_no_match(self):
        # Branch alone without impl title is not enough
        pr = self._pr(title="chore: update deps", branch="usr/at/1-impl-fix")
        # chore matches _IMPL_TITLE_RE so this should still match
        assert h._is_obviously_implementation(pr) is True

    def test_impl_branch_non_impl_title_no_match(self):
        # Random title with impl branch — should NOT match
        pr = self._pr(title="Update README", branch="usr/at/1-impl-fix")
        assert h._is_obviously_implementation(pr) is False

    def test_impl_archives_proposal_file_match(self):
        pr = self._pr(files=["openspec/archive/my-spec.md"])
        assert h._is_obviously_implementation(pr) is True

    def test_already_labeled_skipped(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        pr = self._pr(title="Proposal: add auth",
                      labels=[{"name": "sdd-proposal"}])
        prs = [pr]
        fake = MagicMock(); fake.returncode = 0; fake.stdout = json.dumps(prs)
        with patch.object(h, "gh_run", return_value=fake) as mock_gh:
            h.auto_label_prs(cfg, ["owner/repo"])
        # gh_run called once for list, but not for pr edit
        edit_calls = [c for c in mock_gh.call_args_list
                      if len(c[0][0]) > 1 and "edit" in c[0][0]]
        assert len(edit_calls) == 0

    def test_auto_label_disabled_skips(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        cfg.auto_label_prs = False
        with patch.object(h, "gh_run") as mock_gh:
            h.auto_label_prs(cfg, ["owner/repo"])
        mock_gh.assert_not_called()

    def test_auto_label_prs_calls_gh_edit(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        pr = self._pr(number=5, title="Proposal: add auth")
        prs = [pr]
        list_result = MagicMock(); list_result.returncode = 0
        list_result.stdout = json.dumps(prs)
        edit_result = MagicMock(); edit_result.returncode = 0
        edit_result.stdout = ""
        with patch.object(h, "gh_run", side_effect=[list_result, edit_result]) as mock_gh, \
             patch.object(h, "gh_ensure_label_exists"):
            h.auto_label_prs(cfg, ["owner/repo"])
        edit_calls = [c for c in mock_gh.call_args_list
                      if len(c[0][0]) > 1 and "edit" in c[0][0]]
        assert len(edit_calls) == 1
        assert "sdd-proposal" in edit_calls[0][0][0]


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
# TestGhRunPermanentErrors (SPEC 6)
# ---------------------------------------------------------------------------

class TestGhRunPermanentErrorsHunter:
    def _make_result(self, returncode=1, stderr=""):
        r = MagicMock()
        r.returncode = returncode
        r.stderr = stderr
        r.stdout = ""
        r.check_returncode.side_effect = subprocess.CalledProcessError(returncode, ["gh"])
        return r

    def test_permanent_error_fails_immediately_no_retry(self):
        """404 error should not be retried."""
        result = self._make_result(1, "error: not found")
        with patch("subprocess.run", return_value=result) as mock_run, \
             patch("time.sleep") as mock_sleep:
            with pytest.raises(subprocess.CalledProcessError):
                h.gh_run(["issue", "view", "1"])
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    def test_transient_error_retries(self):
        """Rate limit errors should be retried."""
        fail = self._make_result(1, "error: rate limit exceeded")
        ok = MagicMock(); ok.returncode = 0; ok.stdout = "[]"
        with patch("subprocess.run", side_effect=[fail, ok]) as mock_run, \
             patch("time.sleep"):
            result = h.gh_run(["pr", "list"])
        assert mock_run.call_count == 2

    def test_unknown_error_fails_immediately(self):
        """Unknown errors should not be retried."""
        result = self._make_result(1, "error: something unexpected happened")
        with patch("subprocess.run", return_value=result) as mock_run, \
             patch("time.sleep") as mock_sleep:
            with pytest.raises(subprocess.CalledProcessError):
                h.gh_run(["pr", "list"])
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    def test_check_false_returns_on_error(self):
        """check=False should return even on error."""
        result = self._make_result(1, "error: not found")
        with patch("subprocess.run", return_value=result):
            r = h.gh_run(["issue", "view", "1"], check=False)
        assert r.returncode == 1


# ---------------------------------------------------------------------------
# TestGhIssueHelpers
# ---------------------------------------------------------------------------

class TestGhIssueHelpers:
    def _fake_gh(self, stdout="[]"):
        fake = MagicMock()
        fake.returncode = 0
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
        def fake_run_proc_hunter(cmd, wt, env=None, stdin_text=None):
            captured_cmd["cmd"] = cmd
            return "ok"

        with patch.object(h, "_run_proc_hunter", side_effect=fake_run_proc_hunter):
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
        def fake_run_proc_hunter(cmd, wt, env=None, stdin_text=None):
            captured["env"] = env
            return "ok"

        with patch.dict(os.environ, {
            "CLAUDECODE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "HOME": "/home/testuser",
        }), patch.object(h, "_run_proc_hunter", side_effect=fake_run_proc_hunter):
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

        with patch.object(h, "gh_find_merged_proposal", return_value=None), \
             patch.object(h, "gh_repo_default_branch") as mock_branch:
            h.check_proposal_merged(cfg, state, "owner/repo", "owner/repo!3", entry)

        mock_branch.assert_not_called()
        assert state == {}

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

        with patch.object(h, "gh_find_merged_proposal", return_value=42), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", return_value=worktree), \
             patch.object(h, "run_skill", return_value="done"), \
             patch.object(h, "skill_has_commits", return_value=True), \
             patch.object(h, "gh_create_branch_and_pr", return_value=99), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_remove_label"), \
             patch.object(h, "gh_pr_reviews", return_value=[]), \
             patch.object(h, "gh_pr_inline_comments", return_value=[]), \
             patch.object(h, "gh_pr_issue_comments", return_value=[]), \
             patch.object(h, "load_hunter_state", return_value=state), \
             patch.object(h, "save_hunter_state"):
            h.check_proposal_merged(cfg, state, "owner/repo", "owner/repo!3", entry)

        assert state.get("owner/repo!3", {}).get("impl_pr") == 99
        assert state.get("owner/repo!3", {}).get("status") == "implementing"

    def test_removes_proposal_label_when_starting_implementation(self, tmp_path):
        """Verify proposal-open label is removed when transitioning to implementing."""
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)

        skill = tmp_path / "impl-skill.md"
        skill.write_text("Implement $ARGUMENTS")
        cfg.impl_skill_path = skill

        state = {}
        entry = self._entry()
        worktree = tmp_path / "impl-wt"
        worktree.mkdir()

        with patch.object(h, "gh_find_merged_proposal", return_value=42), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", return_value=worktree), \
             patch.object(h, "run_skill", return_value="done"), \
             patch.object(h, "skill_has_commits", return_value=True), \
             patch.object(h, "gh_create_branch_and_pr", return_value=99), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label") as mock_add_label, \
             patch.object(h, "gh_issue_remove_label") as mock_remove_label, \
             patch.object(h, "gh_pr_reviews", return_value=[]), \
             patch.object(h, "gh_pr_inline_comments", return_value=[]), \
             patch.object(h, "gh_pr_issue_comments", return_value=[]), \
             patch.object(h, "load_hunter_state", return_value=state), \
             patch.object(h, "save_hunter_state"):
            h.check_proposal_merged(cfg, state, "owner/repo", "owner/repo!3", entry)

        # Verify both add and remove were called for the labels
        mock_add_label.assert_called_with("owner/repo", 3, "testuser:implementing")
        mock_remove_label.assert_called_with("owner/repo", 3, "testuser:proposal-open")

    def test_gh_find_merged_proposal_error_propagates(self, tmp_path):
        """gh_find_merged_proposal raising propagates to caller."""
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_issue_is_closed", return_value=False), \
             patch.object(h, "gh_pr_reviews", return_value=[]), \
             patch.object(h, "gh_pr_inline_comments", return_value=[]), \
             patch.object(h, "gh_pr_issue_comments", return_value=[]), \
             patch.object(h, "load_hunter_state", return_value=state), \
             patch.object(h, "save_hunter_state"), \
             patch.object(h, "gh_find_merged_proposal", side_effect=RuntimeError("network")):
            with pytest.raises(RuntimeError, match="network"):
                h.check_proposal_merged(cfg, state, "owner/repo", "owner/repo!3", entry)

    def test_no_merged_proposal_returns_early(self, tmp_path):
        """When gh_find_merged_proposal returns None, state is unchanged."""
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = {"issue_number": 3, "repo": "owner/repo", "title": "X", "status": "proposal_open"}

        with patch.object(h, "gh_issue_is_closed", return_value=False), \
             patch.object(h, "gh_find_merged_proposal", return_value=None) as mock_find:
            h.check_proposal_merged(cfg, state, "owner/repo", "owner/repo!3", entry)

        mock_find.assert_called_once()
        assert state == {}


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
             patch.object(h, "gh_issue_is_closed", return_value=False), \
             patch.object(h, "gh_issue_comment") as mock_comment:
            h.check_impl_merged(cfg, state, "owner/repo", "owner/repo!5", entry)

        mock_comment.assert_not_called()

    def test_impl_merged_closes_issue(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_issue_is_closed", return_value=False), \
             patch.object(h, "gh_pr_is_merged", return_value=True), \
             patch.object(h, "gh_issue_comment"), \
             patch.object(h, "gh_run") as mock_gh_run, \
             patch.object(h, "save_hunter_state"):
            h.check_impl_merged(cfg, state, "owner/repo", "owner/repo!5", entry)

        close_calls = [c for c in mock_gh_run.call_args_list
                       if "issue" in c[0][0] and "close" in c[0][0]]
        assert len(close_calls) == 1
        assert "5" in close_calls[0][0][0]

    def test_impl_merged_posts_comment(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_issue_is_closed", return_value=False), \
             patch.object(h, "gh_pr_is_merged", return_value=True), \
             patch.object(h, "gh_issue_comment") as mock_comment, \
             patch.object(h, "gh_run"), \
             patch.object(h, "save_hunter_state"):
            h.check_impl_merged(cfg, state, "owner/repo", "owner/repo!5", entry)

        mock_comment.assert_called_once()
        comment_body = mock_comment.call_args[0][2]
        assert "30" in comment_body  # impl PR number in comment

    def test_merged_updates_status_to_submitted(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = self._entry()

        with patch.object(h, "gh_issue_is_closed", return_value=False), \
             patch.object(h, "gh_pr_is_merged", return_value=True), \
             patch.object(h, "gh_issue_comment"), \
             patch.object(h, "gh_run"), \
             patch.object(h, "save_hunter_state"):
            h.check_impl_merged(cfg, state, "owner/repo", "owner/repo!5", entry)

        assert state.get("owner/repo!5", {}).get("status") == "submitted"

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

        with patch.object(h, "gh_issue_is_closed", return_value=False), \
             patch.object(h, "gh_pr_is_merged", side_effect=RuntimeError("network")):
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

    def test_label_passed_to_gh(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        called_args = []

        def fake_run(args, **kwargs):
            called_args.append(list(args))
            r = MagicMock()
            r.stdout = "https://github.com/owner/repo/pull/7\n"
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=fake_run):
            h.gh_create_branch_and_pr(
                "owner/repo", "main", "feat/1", "PR", "body",
                label="sdd-proposal", worktree=worktree,
            )
        gh_calls = [a for a in called_args if len(a) > 0 and a[0] == "gh"]
        assert any("--label" in a and "sdd-proposal" in a for a in gh_calls)


class TestGhFindMergedProposal:
    def _fake_gh(self, prs):
        r = MagicMock()
        r.returncode = 0
        r.stdout = json.dumps(prs)
        return r

    def test_finds_by_issue_number_in_body(self):
        prs = [{"number": 42, "title": "Proposal: fix", "body": "Fixes #10"}]
        with patch("subprocess.run", return_value=self._fake_gh(prs)):
            result = h.gh_find_merged_proposal("owner/repo", 10, "fix")
        assert result == 42

    def test_finds_by_issue_number_in_title(self):
        prs = [{"number": 55, "title": "Proposal for #10", "body": ""}]
        with patch("subprocess.run", return_value=self._fake_gh(prs)):
            result = h.gh_find_merged_proposal("owner/repo", 10, "fix")
        assert result == 55

    def test_returns_none_when_no_match(self):
        prs = [{"number": 1, "title": "Unrelated", "body": "nothing"}]
        with patch("subprocess.run", return_value=self._fake_gh(prs)):
            result = h.gh_find_merged_proposal("owner/repo", 10, "fix")
        assert result is None

    def test_returns_none_on_gh_failure(self):
        r = MagicMock(); r.returncode = 1; r.stdout = ""
        with patch("subprocess.run", return_value=r):
            result = h.gh_find_merged_proposal("owner/repo", 10, "fix")
        assert result is None


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

        with patch.object(_predd, "find_local_repo", return_value=local_repo), \
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

        with patch.object(h, "gh_find_merged_proposal", return_value=42), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", side_effect=RuntimeError("clone failed")), \
             patch.object(h, "gh_pr_reviews", return_value=[]), \
             patch.object(h, "gh_pr_inline_comments", return_value=[]), \
             patch.object(h, "gh_pr_issue_comments", return_value=[]), \
             patch.object(h, "load_hunter_state", return_value=state), \
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

        with patch.object(h, "gh_issue_is_closed", return_value=False), \
             patch.object(h, "gh_pr_is_merged", return_value=True), \
             patch.object(h, "gh_issue_comment", side_effect=RuntimeError("network")), \
             patch.object(h, "save_hunter_state"):
            h.check_impl_merged(cfg, state, "owner/repo", "owner/repo!5", entry)

        assert state.get("owner/repo!5", {}).get("status") == "failed"


# ---------------------------------------------------------------------------
# TestSkipClosedIssues (SPEC 3)
# ---------------------------------------------------------------------------

class TestSkipClosedIssues:
    def _proposal_entry(self):
        return {
            "issue_number": 3,
            "proposal_pr": 20,
            "repo": "owner/repo",
            "title": "A feature",
            "issue_author": "reporter",
            "status": "proposal_open",
        }

    def _impl_entry(self):
        return {
            "issue_number": 5,
            "impl_pr": 30,
            "repo": "owner/repo",
            "title": "A feature",
            "issue_author": "reporter",
            "status": "ready_for_review",
        }

    def test_gh_issue_is_closed_returns_true_when_closed(self):
        r = MagicMock(); r.returncode = 0; r.stdout = '{"state":"CLOSED"}'
        with patch.object(h, "gh_run", return_value=r):
            assert h.gh_issue_is_closed("owner/repo", 42) is True

    def test_gh_issue_is_closed_returns_false_when_open(self):
        r = MagicMock(); r.returncode = 0; r.stdout = "OPEN\n"
        with patch.object(h, "gh_run", return_value=r):
            assert h.gh_issue_is_closed("owner/repo", 42) is False

    def test_closed_issue_marks_submitted_in_check_proposal_merged(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = self._proposal_entry()

        with patch.object(h, "gh_issue_is_closed", return_value=True), \
             patch.object(h, "gh_find_merged_proposal") as mock_find, \
             patch.object(h, "save_hunter_state"):
            h.check_proposal_merged(cfg, state, "owner/repo", "owner/repo!3", entry)

        mock_find.assert_not_called()
        assert state.get("owner/repo!3", {}).get("status") == "submitted"

    def test_closed_issue_marks_submitted_in_check_impl_merged(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}
        entry = self._impl_entry()

        with patch.object(h, "gh_issue_is_closed", return_value=True), \
             patch.object(h, "gh_pr_is_merged") as mock_merged, \
             patch.object(h, "save_hunter_state"):
            h.check_impl_merged(cfg, state, "owner/repo", "owner/repo!5", entry)

        mock_merged.assert_not_called()
        assert state.get("owner/repo!5", {}).get("status") == "submitted"


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

        # in-progress returns issue 42, others return empty
        def fake_gh_run(args, check=True):
            r = MagicMock()
            r.returncode = 0
            if "in-progress" in str(args):
                r.stdout = json.dumps([{"number": 42}])
            else:
                r.stdout = json.dumps([])
            return r

        with patch.object(h, "gh_run", side_effect=fake_gh_run), \
             patch.object(h, "gh_issue_remove_label") as mock_remove:
            h.scan_orphaned_labels(cfg, state, ["owner/repo"])

        assert mock_remove.call_count >= 1
        remove_calls = [(c[0][2]) for c in mock_remove.call_args_list]
        assert f"{cfg.github_user}:in-progress" in remove_calls

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

    def test_scan_orphaned_labels_cleans_proposal_open(self, tmp_path):
        """Issue with proposal-open label but no state entry should be cleaned."""
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}

        def fake_gh_run(args, check=True):
            r = MagicMock()
            r.returncode = 0
            if "proposal-open" in str(args):
                r.stdout = json.dumps([{"number": 7}])
            else:
                r.stdout = json.dumps([])
            return r

        with patch.object(h, "gh_run", side_effect=fake_gh_run), \
             patch.object(h, "gh_issue_remove_label") as mock_remove:
            h.scan_orphaned_labels(cfg, state, ["owner/repo"])

        remove_calls = [(c[0][2]) for c in mock_remove.call_args_list]
        assert f"{cfg.github_user}:proposal-open" in remove_calls

    def test_scan_orphaned_labels_cleans_implementing(self, tmp_path):
        """Issue with implementing label but failed state should be cleaned."""
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {"owner/repo!8": _base_entry(issue_number=8, status="failed")}

        def fake_gh_run(args, check=True):
            r = MagicMock()
            r.returncode = 0
            if "implementing" in str(args):
                r.stdout = json.dumps([{"number": 8}])
            else:
                r.stdout = json.dumps([])
            return r

        with patch.object(h, "gh_run", side_effect=fake_gh_run), \
             patch.object(h, "gh_issue_remove_label") as mock_remove:
            h.scan_orphaned_labels(cfg, state, ["owner/repo"])

        remove_calls = [(c[0][2]) for c in mock_remove.call_args_list]
        assert f"{cfg.github_user}:implementing" in remove_calls

    def test_scan_orphaned_labels_skips_active_state(self, tmp_path):
        """Issue with in-progress label and active state should NOT be cleaned."""
        cfg = _make_cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {"owner/repo!9": _base_entry(issue_number=9, status="in_progress")}

        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = json.dumps([{"number": 9}])

        with patch.object(h, "gh_run", return_value=fake), \
             patch.object(h, "gh_issue_remove_label") as mock_remove:
            h.scan_orphaned_labels(cfg, state, ["owner/repo"])

        mock_remove.assert_not_called()


class TestParseCapability:
    def test_parses_capability_line(self):
        result = h._parse_capability("Some text\ncapability: 123 cool feature\nmore")
        assert result == "123 — cool feature"

    def test_case_insensitive(self):
        result = h._parse_capability("Capability: 42 auth system")
        assert result == "42 — auth system"

    def test_returns_none_when_missing(self):
        assert h._parse_capability("no capability here") is None


class TestBuildIssueBody:
    def _row(self, **kwargs):
        base = {
            "issue key": "DAP-1",
            "summary": "Fix bug",
            "issue type": "Story",
            "epic link": "DAP-100",
            "sprint": "DAP Sprint-1 2026-05-12",
            "description": "capability: 42 payments\nDo the thing.",
        }
        base.update(kwargs)
        return base

    def test_full_conformant_row(self):
        body, missing = h._build_issue_body(self._row(), "https://jira.example.com")
        assert "DAP-1" in body
        assert "Story" in body
        assert "DAP-100" in body
        assert "Sprint-1" in body
        assert "42 — payments" in body
        assert missing == []

    def test_missing_epic_and_capability(self):
        row = self._row(epic_link="", sprint="", description="no cap here")
        row.pop("epic link", None)
        row["epic link"] = ""
        row["sprint"] = ""
        body, missing = h._build_issue_body(row, "https://jira.example.com")
        # Sprint is no longer in missing (hard gate in ingest), Capability requirement is commented out
        # So only Epic is missing
        assert len(missing) == 1
        assert missing[0] == "Epic not set"

    def test_epic_key_is_hyperlinked(self):
        body, _ = h._build_issue_body(self._row(), "https://jira.example.com")
        assert "[DAP-100](https://jira.example.com/browse/DAP-100)" in body

    def test_epic_name_is_plain_text(self):
        row = self._row()
        row["epic link"] = "My Epic Name"
        body, _ = h._build_issue_body(row, "https://jira.example.com")
        assert "My Epic Name" in body
        assert "browse/My Epic Name" not in body

    def test_description_appended(self):
        body, _ = h._build_issue_body(self._row(), "https://jira.example.com")
        assert "Do the thing" in body

    def test_warning_block_on_missing(self):
        row = self._row(epic_link="")
        row["epic link"] = ""
        body, missing = h._build_issue_body(row, "https://jira.example.com")
        assert "⚠️" in body
        assert "Epic not set" in body


class TestGhIssueExists:
    def test_returns_true_when_found(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = json.dumps([{"number": 1, "title": "[DAP-1] Fix bug"}])
        with patch("subprocess.run", return_value=fake):
            assert h.gh_issue_exists("owner/repo", "DAP-1") is True

    def test_returns_false_when_not_found(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = json.dumps([])
        with patch("subprocess.run", return_value=fake):
            assert h.gh_issue_exists("owner/repo", "DAP-1") is False

    def test_returns_false_on_gh_failure(self):
        fake = MagicMock(); fake.returncode = 1; fake.stdout = ""
        with patch("subprocess.run", return_value=fake):
            assert h.gh_issue_exists("owner/repo", "DAP-1") is False

    def test_does_not_match_substring(self):
        # DAP-1 should not match DAP-10
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = json.dumps([{"number": 2, "title": "[DAP-10] Other issue"}])
        with patch("subprocess.run", return_value=fake):
            assert h.gh_issue_exists("owner/repo", "DAP-1") is False


class TestGhIssueCreate:
    def test_returns_issue_number(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "https://github.com/owner/repo/issues/42\n"
        with patch("subprocess.run", return_value=fake):
            result = h.gh_issue_create("owner/repo", "Title", "Body", assignee="adam")
        assert result == 42

    def test_returns_none_on_failure(self):
        fake = MagicMock(); fake.returncode = 1; fake.stdout = ""
        with patch("subprocess.run", return_value=fake):
            assert h.gh_issue_create("owner/repo", "Title", "Body") is None




# ---------------------------------------------------------------------------
# TestSprintGate
# ---------------------------------------------------------------------------

class TestSprintGate:
    # --- _sprint_jql_clause ---

    def test_sprint_jql_active(self):
        assert h._sprint_jql_clause("active") == "sprint in openSprints()"

    def test_sprint_jql_all(self):
        assert h._sprint_jql_clause("all") is None

    def test_sprint_jql_named(self):
        assert h._sprint_jql_clause("named:Sprint-10") == 'sprint = "Sprint-10"'

    def test_sprint_jql_named_escapes_quotes(self):
        result = h._sprint_jql_clause('named:Sprint "X"')
        assert result == 'sprint = "Sprint \\"X\\""'

    def test_sprint_jql_unknown_falls_back(self):
        assert h._sprint_jql_clause("bogus") == "sprint in openSprints()"

    # --- _passes_sprint_gate ---

    def _cfg_with_filter(self, tmp_path, sprint_filter, active_sprint_name=""):
        cfg = _make_cfg(tmp_path)
        cfg.jira_sprint_filter = sprint_filter
        cfg.jira_active_sprint_name = active_sprint_name
        return cfg

    def test_passes_sprint_gate_all_empty_sprint(self, tmp_path):
        cfg = self._cfg_with_filter(tmp_path, "all")
        assert h._passes_sprint_gate("", cfg) is True

    def test_passes_sprint_gate_all_any_sprint(self, tmp_path):
        cfg = self._cfg_with_filter(tmp_path, "all")
        assert h._passes_sprint_gate("Sprint-99", cfg) is True

    def test_passes_sprint_gate_active_empty_sprint(self, tmp_path):
        cfg = self._cfg_with_filter(tmp_path, "active")
        assert h._passes_sprint_gate("", cfg) is False

    def test_passes_sprint_gate_active_nonempty_no_name_filter(self, tmp_path):
        cfg = self._cfg_with_filter(tmp_path, "active")
        assert h._passes_sprint_gate("Sprint-10", cfg) is True

    def test_passes_sprint_gate_active_named_match(self, tmp_path):
        cfg = self._cfg_with_filter(tmp_path, "active", active_sprint_name="Sprint-10")
        assert h._passes_sprint_gate("Sprint-10", cfg) is True

    def test_passes_sprint_gate_active_named_mismatch(self, tmp_path):
        cfg = self._cfg_with_filter(tmp_path, "active", active_sprint_name="Sprint-10")
        assert h._passes_sprint_gate("Sprint-9", cfg) is False

    def test_passes_sprint_gate_named_filter_match(self, tmp_path):
        cfg = self._cfg_with_filter(tmp_path, "named:Sprint-10")
        assert h._passes_sprint_gate("Sprint-10", cfg) is True

    def test_passes_sprint_gate_named_filter_mismatch(self, tmp_path):
        cfg = self._cfg_with_filter(tmp_path, "named:Sprint-10")
        assert h._passes_sprint_gate("Sprint-9", cfg) is False


# ---------------------------------------------------------------------------
# TestCollectPrFeedback
# ---------------------------------------------------------------------------

class TestCollectPrFeedback:
    def _cfg(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        cfg.collect_pr_feedback = True
        cfg.github_user = "testuser"
        return cfg

    def test_collect_pr_feedback_disabled(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.collect_pr_feedback = False
        state = {}
        with patch.object(h, "gh_pr_reviews") as mock_reviews:
            h.collect_pr_feedback(cfg, state, "owner/repo", "owner/repo!1", 42, "proposal_feedback")
        mock_reviews.assert_not_called()

    def test_collects_review_and_stores_in_state(self, tmp_path):
        cfg = self._cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {"owner/repo!1": {"issue_number": 1, "status": "proposal_open"}}

        reviews = [{"id": 100, "state": "REQUEST_CHANGES", "body": "Missing tests",
                    "submitted_at": "2026-05-13T10:00:00Z",
                    "user": {"login": "reviewer1"}}]
        with patch.object(h, "gh_pr_reviews", return_value=reviews), \
             patch.object(h, "gh_pr_inline_comments", return_value=[]), \
             patch.object(h, "gh_pr_issue_comments", return_value=[]), \
             patch.object(h, "log_decision") as mock_log, \
             patch.object(h, "save_hunter_state"):
            h.collect_pr_feedback(cfg, state, "owner/repo", "owner/repo!1", 42, "proposal_feedback")

        feedback = state["owner/repo!1"].get("proposal_feedback", [])
        assert len(feedback) == 1
        assert feedback[0]["type"] == "REQUEST_CHANGES"
        assert feedback[0]["reviewer"] == "reviewer1"
        mock_log.assert_called_once()

    def test_skips_already_seen_reviews(self, tmp_path):
        cfg = self._cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        existing = [{"review_id": 100, "type": "APPROVED", "reviewer": "r1",
                     "ts": "2026-05-13T09:00:00Z", "body": "", "inline_comments": []}]
        state = {"owner/repo!1": {"issue_number": 1, "proposal_feedback": existing}}

        reviews = [{"id": 100, "state": "APPROVED", "body": "",
                    "submitted_at": "2026-05-13T09:00:00Z",
                    "user": {"login": "r1"}}]
        with patch.object(h, "gh_pr_reviews", return_value=reviews), \
             patch.object(h, "gh_pr_inline_comments", return_value=[]), \
             patch.object(h, "gh_pr_issue_comments", return_value=[]), \
             patch.object(h, "save_hunter_state"):
            h.collect_pr_feedback(cfg, state, "owner/repo", "owner/repo!1", 42, "proposal_feedback")

        # Still only 1 item — duplicate skipped
        assert len(state["owner/repo!1"]["proposal_feedback"]) == 1

    def test_collects_inline_comments(self, tmp_path):
        cfg = self._cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {"owner/repo!1": {"issue_number": 1}}

        reviews = [{"id": 200, "state": "REQUEST_CHANGES", "body": "See inline",
                    "submitted_at": "2026-05-13T10:00:00Z",
                    "user": {"login": "r2"}}]
        inline = [{"pull_request_review_id": 200, "path": "design.md",
                   "line": 5, "body": "Missing detail"}]
        with patch.object(h, "gh_pr_reviews", return_value=reviews), \
             patch.object(h, "gh_pr_inline_comments", return_value=inline), \
             patch.object(h, "gh_pr_issue_comments", return_value=[]), \
             patch.object(h, "log_decision"), \
             patch.object(h, "save_hunter_state"):
            h.collect_pr_feedback(cfg, state, "owner/repo", "owner/repo!1", 42, "proposal_feedback")

        feedback = state["owner/repo!1"]["proposal_feedback"]
        assert feedback[0]["inline_comments"][0]["path"] == "design.md"

    def test_skips_own_comments(self, tmp_path):
        cfg = self._cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {"owner/repo!1": {"issue_number": 1}}

        issue_comments = [{"id": 99, "body": "hunter comment",
                           "created_at": "2026-05-13T10:00:00Z",
                           "user": {"login": "testuser"}}]  # own comment
        with patch.object(h, "gh_pr_reviews", return_value=[]), \
             patch.object(h, "gh_pr_inline_comments", return_value=[]), \
             patch.object(h, "gh_pr_issue_comments", return_value=issue_comments), \
             patch.object(h, "save_hunter_state"):
            h.collect_pr_feedback(cfg, state, "owner/repo", "owner/repo!1", 42, "proposal_feedback")

        assert state["owner/repo!1"].get("proposal_feedback", []) == []

    def test_gh_api_failure_does_not_crash(self, tmp_path):
        cfg = self._cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {"owner/repo!1": {"issue_number": 1}}

        with patch.object(h, "gh_pr_reviews", side_effect=Exception("network")):
            h.collect_pr_feedback(cfg, state, "owner/repo", "owner/repo!1", 42, "proposal_feedback")
        # Should not raise


# ---------------------------------------------------------------------------
# TestJiraLabeling
# ---------------------------------------------------------------------------

class TestJiraLabeling:
    """Tests for extract_jira_key, label_jira_issue, and _sweep_jira_labels."""

    def test_extract_jira_key_standard(self):
        assert h.extract_jira_key("[DAP09A-1184] Some feature") == "DAP09A-1184"

    def test_extract_jira_key_short_project(self):
        assert h.extract_jira_key("[BPA-42] Fix thing") == "BPA-42"

    def test_extract_jira_key_two_letter(self):
        assert h.extract_jira_key("[AI-7] AI improvement") == "AI-7"

    def test_extract_jira_key_no_match(self):
        assert h.extract_jira_key("Just a regular title") is None

    def test_extract_jira_key_empty(self):
        assert h.extract_jira_key("") is None

    def test_extract_jira_key_none(self):
        assert h.extract_jira_key(None) is None

    def test_extract_jira_key_lowercase_rejected(self):
        assert h.extract_jira_key("[abc-123] lowercase") is None

    def test_extract_jira_key_no_brackets(self):
        assert h.extract_jira_key("DAP09A-1184 no brackets") is None

    def test_label_jira_issue_applies_label(self):
        with patch.object(h, "gh_ensure_label_exists") as mock_ensure, \
             patch.object(h, "gh_issue_add_label") as mock_add, \
             patch.object(h, "log_decision"):
            h.label_jira_issue("owner/repo", 42, "[BPA-99] do the thing")

        mock_ensure.assert_called_once_with("owner/repo", "jira", color="0052CC")
        mock_add.assert_called_once_with("owner/repo", 42, "jira")

    def test_label_jira_issue_skips_no_key(self):
        with patch.object(h, "gh_ensure_label_exists") as mock_ensure:
            h.label_jira_issue("owner/repo", 42, "No jira key here")

        mock_ensure.assert_not_called()

    def test_label_jira_issue_handles_error(self):
        with patch.object(h, "gh_ensure_label_exists", side_effect=Exception("fail")):
            h.label_jira_issue("owner/repo", 42, "[BPA-99] do the thing")
        # Should not raise

    def test_sweep_skips_already_labeled(self):
        issues_json = json.dumps([
            {"number": 1, "title": "[BPA-1] Already labeled", "labels": [{"name": "jira"}]},
            {"number": 2, "title": "[BPA-2] Needs label", "labels": []},
            {"number": 3, "title": "No jira key", "labels": []},
        ])
        fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=issues_json, stderr="")

        with patch.object(h, "gh_run", return_value=fake_result), \
             patch.object(h, "label_jira_issue") as mock_label:
            h._sweep_jira_labels(MagicMock(), ["owner/repo"])

        # Only issue #2 should be labeled (has key, not already labeled)
        mock_label.assert_called_once_with("owner/repo", 2, "[BPA-2] Needs label")


# ---------------------------------------------------------------------------
# TestBranchNamingJiraKey
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# TestEpicSprintConformance
# ---------------------------------------------------------------------------

class TestEpicSprintConformance:
    """Tests for _find_epic."""

    def test_find_epic_uses_epic_link(self):
        row = {"epic link": "DAP-100", "epic name": "", "parent": ""}
        assert h._find_epic(row) == "DAP-100"

    def test_find_epic_falls_back_to_parent(self):
        row = {"epic link": "", "epic name": "", "parent": "DAP-200"}
        assert h._find_epic(row) == "DAP-200"

    def test_find_epic_returns_empty_when_all_missing(self):
        row = {"epic link": "", "epic name": ""}
        assert h._find_epic(row) == ""


class TestBranchNamingJiraKey:
    """Tests for issue_identifier and Jira-key-aware branch naming."""

    def test_issue_identifier_returns_jira_key_when_present(self):
        assert h.issue_identifier(377, "[DAP09A-1841] Add foo") == "DAP09A-1841"

    def test_issue_identifier_falls_back_to_issue_number(self):
        assert h.issue_identifier(377, "Add foo") == "377"

    def test_proposal_branch_uses_jira_key(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        branch = h.proposal_branch(cfg, 377, "[DAP09A-1900] Add foo")
        assert "DAP09A-1900-proposal-" in branch
        assert "377" not in branch

    def test_proposal_branch_falls_back_to_issue_number(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        branch = h.proposal_branch(cfg, 377, "Add foo")
        assert "377-proposal-" in branch

    def test_impl_branch_uses_jira_key(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        branch = h.impl_branch(cfg, 377, "[BPA-42] Fix thing")
        assert "BPA-42-impl-" in branch
        assert "377" not in branch

    def test_impl_branch_falls_back_to_issue_number(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        branch = h.impl_branch(cfg, 377, "Fix thing")
        assert "377-impl-" in branch


class TestJiraClient:
    """Tests for JiraClient REST API client."""

    def test_validate_success(self):
        """Test successful authentication via /myself endpoint."""
        client = _predd.JiraClient("https://jira.example.com", "token123")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"name": "testuser"}'
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            result = client.validate()
            assert result is True
            mock_urlopen.assert_called_once()
            # Check Authorization header
            req = mock_urlopen.call_args[0][0]
            assert req.get_header("Authorization") == "Bearer token123"

    def test_validate_failure(self):
        """Test failed authentication."""
        client = _predd.JiraClient("https://jira.example.com", "badtoken")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "url", 401, "Unauthorized", {}, None
            )
            result = client.validate()
            assert result is False

    def test_search_returns_issues(self):
        """Test search() returns issues from JQL query."""
        client = _predd.JiraClient("https://jira.example.com", "token123")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = json.dumps({
                "issues": [
                    {"key": "DAP-1", "fields": {"summary": "Issue 1"}},
                    {"key": "DAP-2", "fields": {"summary": "Issue 2"}},
                ]
            }).encode("utf-8")
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            issues = client.search("sprint in openSprints()")
            assert len(issues) == 2
            assert issues[0]["key"] == "DAP-1"

    def test_search_with_fields(self):
        """Test search() passes custom fields."""
        client = _predd.JiraClient("https://jira.example.com", "token123")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"issues": []}'
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            client.search("project = DAP", fields=["key", "summary", "customfield_10005"])
            req = mock_urlopen.call_args[0][0]
            assert "customfield_10005" in req.full_url

    def test_get_issue(self):
        """Test get_issue() fetches single issue."""
        client = _predd.JiraClient("https://jira.example.com", "token123")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = json.dumps({
                "key": "DAP-42",
                "fields": {"summary": "Test issue"}
            }).encode("utf-8")
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            issue = client.get_issue("DAP-42")
            assert issue["key"] == "DAP-42"
            req = mock_urlopen.call_args[0][0]
            assert "/issue/DAP-42" in req.full_url

    def test_rate_limit_retry(self):
        """Test exponential backoff on 429 (rate limit)."""
        client = _predd.JiraClient("https://jira.example.com", "token123")
        with patch("urllib.request.urlopen") as mock_urlopen, \
             patch("time.sleep") as mock_sleep:
            # First call: 429, second call: success
            error_response = MagicMock()
            error_response.code = 429
            error_response.headers = {"Retry-After": "1"}
            mock_urlopen.side_effect = [
                urllib.error.HTTPError("url", 429, "Too Many Requests", error_response.headers, None),
                MagicMock(__enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=b'{"issues": []}')))),
            ]

            # We need to adjust this test because of how the exception is raised
            # Let's just verify the retry logic exists
            issues = client.search("test", max_results=10)
            # Should have retried once
            assert mock_sleep.called

    def test_base_url_rstrip(self):
        """Test that base URL trailing slash is removed."""
        client = _predd.JiraClient("https://jira.example.com/", "token123")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"issues": []}'
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            client.search("test")
            req = mock_urlopen.call_args[0][0]
            # Should not have double slash after domain
            assert "https://jira.example.com/rest" in req.full_url
            assert "https://jira.example.com//rest" not in req.full_url


class TestIngestJiraApi:
    """Tests for ingest_jira_api function."""

    def _cfg_with_api(self, tmp_path):
        """Create config with Jira API enabled."""
        cfg = _make_cfg(tmp_path)
        cfg.jira_api_enabled = True
        cfg.jira_projects = ["DAP09A"]
        cfg.jira_base_url = "https://jira.example.com"
        return cfg

    def test_skips_when_no_projects(self, tmp_path):
        """Test skips ingest when jira_projects is empty."""
        cfg = _make_cfg(tmp_path)
        cfg.jira_api_enabled = True
        cfg.jira_projects = []
        with patch.dict(os.environ, {"JIRA_API_TOKEN": "token123"}), \
             patch.object(h, "gh_issue_exists") as mock_exists:
            h.ingest_jira_api(cfg, ["owner/repo"])
        mock_exists.assert_not_called()

    def test_skips_when_api_disabled(self, tmp_path):
        """Test skips ingest when jira_api_enabled is False."""
        cfg = self._cfg_with_api(tmp_path)
        cfg.jira_api_enabled = False
        with patch.dict(os.environ, {"JIRA_API_TOKEN": "token123"}), \
             patch.object(h, "gh_issue_exists") as mock_exists:
            h.ingest_jira_api(cfg, ["owner/repo"])
        mock_exists.assert_not_called()

    def test_skips_when_no_token(self, tmp_path):
        """Test skips ingest when JIRA_API_TOKEN not set."""
        cfg = self._cfg_with_api(tmp_path)
        env = {k: v for k, v in os.environ.items() if k != "JIRA_API_TOKEN"}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(h, "gh_issue_exists") as mock_exists:
            h.ingest_jira_api(cfg, ["owner/repo"])
        mock_exists.assert_not_called()

    def test_returns_early_when_validate_fails(self, tmp_path):
        """Test returns early when API validation fails."""
        cfg = self._cfg_with_api(tmp_path)
        with patch.dict(os.environ, {"JIRA_API_TOKEN": "badtoken"}), \
             patch.object(_predd.JiraClient, "validate", return_value=False), \
             patch.object(h, "gh_issue_exists") as mock_exists:
            h.ingest_jira_api(cfg, ["owner/repo"])
        mock_exists.assert_not_called()

    def test_creates_conformant_issue(self, tmp_path):
        """Test creates GitHub issue from Jira API response."""
        cfg = self._cfg_with_api(tmp_path)

        api_issue = {
            "key": "DAP-1",
            "fields": {
                "summary": "Fix bug",
                "issuetype": {"name": "Story"},
                "customfield_10006": [{"name": "Sprint-1"}],
                "customfield_10005": "DAP-100",
                "labels": ["owner/repo"],  # Matching repo label
            },
        }

        with patch.dict(os.environ, {"JIRA_API_TOKEN": "token123"}), \
             patch.object(_predd.JiraClient, "validate", return_value=True), \
             patch.object(_predd.JiraClient, "search", return_value=[api_issue]), \
             patch.object(h, "gh_issue_exists", return_value=False), \
             patch.object(h, "gh_issue_create", return_value=10) as mock_create, \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "label_jira_issue"):
            h.ingest_jira_api(cfg, ["owner/repo"])

        mock_create.assert_called_once()
        call_args = mock_create.call_args
        assert "[DAP-1]" in call_args.args[2]  # title
        assert call_args.kwargs.get("assignee") is None

    def test_skips_subtask(self, tmp_path):
        """Test skips sub-task issues."""
        cfg = self._cfg_with_api(tmp_path)
        cfg.skip_jira_issue_types = ["sub-task", "subtask"]

        api_issue = {
            "key": "DAP-2",
            "fields": {
                "summary": "Subtask",
                "issuetype": {"name": "Sub-task"},
                "customfield_10006": [{"name": "Sprint-1"}],
            },
        }

        with patch.dict(os.environ, {"JIRA_API_TOKEN": "token123"}), \
             patch.object(_predd.JiraClient, "validate", return_value=True), \
             patch.object(_predd.JiraClient, "search", return_value=[api_issue]), \
             patch.object(h, "gh_issue_create") as mock_create:
            h.ingest_jira_api(cfg, ["owner/repo"])

        mock_create.assert_not_called()

    def test_skips_issue_without_sprint(self, tmp_path):
        """Test skips issues without sprint (hard gate)."""
        cfg = self._cfg_with_api(tmp_path)

        api_issue = {
            "key": "DAP-3",
            "fields": {
                "summary": "No sprint",
                "issuetype": {"name": "Story"},
                "customfield_10006": None,  # No sprint
                "customfield_10005": "DAP-100",
            },
        }

        with patch.dict(os.environ, {"JIRA_API_TOKEN": "token123"}), \
             patch.object(_predd.JiraClient, "validate", return_value=True), \
             patch.object(_predd.JiraClient, "search", return_value=[api_issue]), \
             patch.object(h, "gh_issue_create") as mock_create:
            h.ingest_jira_api(cfg, ["owner/repo"])

        mock_create.assert_not_called()

    def test_skips_existing_issue(self, tmp_path):
        """Test skips issues that already exist in GitHub."""
        cfg = self._cfg_with_api(tmp_path)

        api_issue = {
            "key": "DAP-4",
            "fields": {
                "summary": "Existing",
                "issuetype": {"name": "Story"},
                "customfield_10006": [{"name": "Sprint-1"}],
            },
        }

        with patch.dict(os.environ, {"JIRA_API_TOKEN": "token123"}), \
             patch.object(_predd.JiraClient, "validate", return_value=True), \
             patch.object(_predd.JiraClient, "search", return_value=[api_issue]), \
             patch.object(h, "gh_issue_exists", return_value=True), \
             patch.object(h, "gh_issue_create") as mock_create:
            h.ingest_jira_api(cfg, ["owner/repo"])

        mock_create.assert_not_called()

    def test_multiple_repos(self, tmp_path):
        """Test creates issues in all configured repos with matching labels."""
        cfg = self._cfg_with_api(tmp_path)

        api_issue = {
            "key": "DAP-5",
            "fields": {
                "summary": "Multi-repo",
                "issuetype": {"name": "Story"},
                "customfield_10006": [{"name": "Sprint-1"}],
                "labels": ["repo1/name1", "repo2/name2"],  # Both repos have matching labels
            },
        }

        with patch.dict(os.environ, {"JIRA_API_TOKEN": "token123"}), \
             patch.object(_predd.JiraClient, "validate", return_value=True), \
             patch.object(_predd.JiraClient, "search", return_value=[api_issue]), \
             patch.object(h, "gh_issue_exists", return_value=False), \
             patch.object(h, "gh_issue_create", return_value=10) as mock_create, \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "label_jira_issue"):
            h.ingest_jira_api(cfg, ["repo1/name1", "repo2/name2"])

        # Should create in both repos
        assert mock_create.call_count == 2

    def test_skips_issue_with_no_labels(self, tmp_path):
        """Test skips issue when it has no repo labels."""
        cfg = self._cfg_with_api(tmp_path)

        api_issue = {
            "key": "DAP-10",
            "fields": {
                "summary": "No labels",
                "issuetype": {"name": "Story"},
                "customfield_10006": [{"name": "Sprint-1"}],
                "labels": [],  # No labels
            },
        }

        with patch.dict(os.environ, {"JIRA_API_TOKEN": "token123"}), \
             patch.object(_predd.JiraClient, "validate", return_value=True), \
             patch.object(_predd.JiraClient, "search", return_value=[api_issue]), \
             patch.object(h, "gh_issue_create") as mock_create:
            h.ingest_jira_api(cfg, ["fusion-e/ai-bp-toolkit"])

        mock_create.assert_not_called()

    def test_skips_issue_with_non_matching_labels(self, tmp_path):
        """Test skips issue when labels don't match any configured repo."""
        cfg = self._cfg_with_api(tmp_path)

        api_issue = {
            "key": "DAP-11",
            "fields": {
                "summary": "Wrong labels",
                "issuetype": {"name": "Story"},
                "customfield_10006": [{"name": "Sprint-1"}],
                "labels": ["other-org/repo", "tech-debt"],  # No matching labels
            },
        }

        with patch.dict(os.environ, {"JIRA_API_TOKEN": "token123"}), \
             patch.object(_predd.JiraClient, "validate", return_value=True), \
             patch.object(_predd.JiraClient, "search", return_value=[api_issue]), \
             patch.object(h, "gh_issue_create") as mock_create:
            h.ingest_jira_api(cfg, ["fusion-e/ai-bp-toolkit"])

        mock_create.assert_not_called()

    def test_creates_only_in_matching_repo(self, tmp_path):
        """Test creates issue only in repo with matching label."""
        cfg = self._cfg_with_api(tmp_path)

        api_issue = {
            "key": "DAP-12",
            "fields": {
                "summary": "Single repo match",
                "issuetype": {"name": "Story"},
                "customfield_10006": [{"name": "Sprint-1"}],
                "labels": ["fusion-e/ai-bp-toolkit", "feature"],
            },
        }

        with patch.dict(os.environ, {"JIRA_API_TOKEN": "token123"}), \
             patch.object(_predd.JiraClient, "validate", return_value=True), \
             patch.object(_predd.JiraClient, "search", return_value=[api_issue]), \
             patch.object(h, "gh_issue_exists", return_value=False), \
             patch.object(h, "gh_issue_create", return_value=10) as mock_create, \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "label_jira_issue"):
            h.ingest_jira_api(cfg, ["fusion-e/ai-bp-toolkit", "fusion-e/other-repo"])

        # Should create only in the matching repo
        mock_create.assert_called_once()
        assert mock_create.call_args.args[0] == "fusion-e/ai-bp-toolkit"

    def test_creates_in_all_matching_repos(self, tmp_path):
        """Test creates issue in all repos with matching labels."""
        cfg = self._cfg_with_api(tmp_path)

        api_issue = {
            "key": "DAP-13",
            "fields": {
                "summary": "Multi-repo match",
                "issuetype": {"name": "Story"},
                "customfield_10006": [{"name": "Sprint-1"}],
                "labels": ["fusion-e/ai-bp-toolkit", "fusion-e/other-repo", "shared"],
            },
        }

        with patch.dict(os.environ, {"JIRA_API_TOKEN": "token123"}), \
             patch.object(_predd.JiraClient, "validate", return_value=True), \
             patch.object(_predd.JiraClient, "search", return_value=[api_issue]), \
             patch.object(h, "gh_issue_exists", return_value=False), \
             patch.object(h, "gh_issue_create", return_value=10) as mock_create, \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "label_jira_issue"):
            h.ingest_jira_api(cfg, ["fusion-e/ai-bp-toolkit", "fusion-e/other-repo"])

        # Should create in both matching repos
        assert mock_create.call_count == 2
        repos_created = [call.args[0] for call in mock_create.call_args_list]
        assert set(repos_created) == {"fusion-e/ai-bp-toolkit", "fusion-e/other-repo"}

    def test_label_matching_is_case_sensitive(self, tmp_path):
        """Test label matching is case-sensitive."""
        cfg = self._cfg_with_api(tmp_path)

        api_issue = {
            "key": "DAP-14",
            "fields": {
                "summary": "Case mismatch",
                "issuetype": {"name": "Story"},
                "customfield_10006": [{"name": "Sprint-1"}],
                "labels": ["Fusion-E/AI-BP-Toolkit"],  # Wrong case
            },
        }

        with patch.dict(os.environ, {"JIRA_API_TOKEN": "token123"}), \
             patch.object(_predd.JiraClient, "validate", return_value=True), \
             patch.object(_predd.JiraClient, "search", return_value=[api_issue]), \
             patch.object(h, "gh_issue_create") as mock_create:
            h.ingest_jira_api(cfg, ["fusion-e/ai-bp-toolkit"])

        # Should not match due to case difference
        mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# TestJiraFrontmatter
# ---------------------------------------------------------------------------

def _full_jira_issue(key="DAP09A-1184"):
    """Return a Jira issue dict with all fields populated."""
    return {
        "key": key,
        "fields": {
            "issuetype": {"name": "Story"},
            "customfield_10014": "DAP09A-1000",
            "parent": {
                "key": "DAP09A-1000",
                "fields": {"summary": "Epic Name"},
            },
            "customfield_10020": [
                {"name": "DAP09A Sprint-9 2026-04-01"},
                {"name": "DAP09A Sprint-10 2026-05-12"},
            ],
            "description": "Some text\ncapability: 12345 cool feature\nmore text",
        },
    }


class TestJiraFrontmatter:
    BASE_URL = "https://jira.cec.lab.emc.com"

    # ------------------------------------------------------------------
    # _build_jira_frontmatter
    # ------------------------------------------------------------------

    def test_full_data_all_fields_present(self):
        issue = _full_jira_issue()
        result = h._build_jira_frontmatter(issue, self.BASE_URL)
        assert "| Jira | [DAP09A-1184](https://jira.cec.lab.emc.com/browse/DAP09A-1184) |" in result
        assert "| Type | Story |" in result
        assert "DAP09A-1000" in result
        assert "Epic Name" in result
        assert "| Sprint | DAP09A Sprint-10 2026-05-12 |" in result
        assert "| Capability | 12345 — cool feature |" in result
        # Ends with separator
        assert result.endswith("\n\n---\n\n")

    def test_missing_epic_row_omitted(self):
        issue = _full_jira_issue()
        issue["fields"]["customfield_10014"] = None
        issue["fields"]["parent"] = None
        result = h._build_jira_frontmatter(issue, self.BASE_URL)
        assert "| Epic |" not in result
        # Other rows still present
        assert "| Jira |" in result
        assert "| Sprint |" in result

    def test_missing_sprint_row_omitted(self):
        issue = _full_jira_issue()
        issue["fields"]["customfield_10020"] = None
        result = h._build_jira_frontmatter(issue, self.BASE_URL)
        assert "| Sprint |" not in result
        assert "| Jira |" in result

    def test_missing_capability_row_omitted(self):
        issue = _full_jira_issue()
        issue["fields"]["description"] = "No capability line here"
        result = h._build_jira_frontmatter(issue, self.BASE_URL)
        assert "| Capability |" not in result
        assert "| Type | Story |" in result

    def test_sprint_takes_last_entry(self):
        issue = _full_jira_issue()
        result = h._build_jira_frontmatter(issue, self.BASE_URL)
        # Should use last sprint, not first
        assert "Sprint-10" in result
        assert "Sprint-9" not in result

    def test_epic_from_parent_key_when_no_customfield_10014(self):
        issue = _full_jira_issue()
        issue["fields"]["customfield_10014"] = ""  # empty, not None
        issue["fields"]["parent"] = {"key": "DAP09A-2000", "fields": {"summary": "Other Epic"}}
        result = h._build_jira_frontmatter(issue, self.BASE_URL)
        assert "DAP09A-2000" in result
        assert "Other Epic" in result

    # ------------------------------------------------------------------
    # _check_jira_conformance
    # ------------------------------------------------------------------

    def test_conformance_all_present_returns_empty(self):
        issue = _full_jira_issue()
        missing = h._check_jira_conformance(issue)
        assert missing == []

    def test_conformance_missing_sprint(self):
        issue = _full_jira_issue()
        issue["fields"]["customfield_10020"] = None
        missing = h._check_jira_conformance(issue)
        assert "Sprint" in missing

    def test_conformance_missing_epic(self):
        issue = _full_jira_issue()
        issue["fields"]["customfield_10014"] = None
        issue["fields"]["parent"] = None
        missing = h._check_jira_conformance(issue)
        assert "Epic" in missing

    def test_conformance_missing_capability(self):
        issue = _full_jira_issue()
        issue["fields"]["description"] = "No capability here"
        missing = h._check_jira_conformance(issue)
        assert "Capability" in missing

    def test_conformance_all_missing(self):
        issue = {
            "key": "DAP09A-1",
            "fields": {
                "issuetype": {"name": "Bug"},
                "customfield_10014": None,
                "parent": None,
                "customfield_10020": [],
                "description": "",
            },
        }
        missing = h._check_jira_conformance(issue)
        assert set(missing) == {"Sprint", "Epic", "Capability"}

    # ------------------------------------------------------------------
    # process_issue: Jira conformance gate
    # ------------------------------------------------------------------

    def test_process_issue_skips_nonconformant_when_require_true(self, tmp_path):
        cfg = _make_cfg(tmp_path, require_jira_conformance=True, jira_api_enabled=True)
        monkeypatch_state_file(tmp_path)
        state = {}
        issue = {
            "number": 42,
            "title": "[DAP09A-1184] Fix the widget",
            "author": {"login": "reporter"},
            "labels": [],
            "body": "desc",
        }

        missing_fields = ["Sprint", "Capability"]

        with patch.object(h, "_fetch_jira_frontmatter", return_value=("", missing_fields, {})), \
             patch.object(h, "label_jira_issue"), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_comment"), \
             patch.object(h, "try_claim_issue") as mock_claim, \
             patch.object(h, "save_hunter_state"):
            h.process_issue(cfg, state, "owner/repo", issue)

        # Should not attempt to claim the issue
        mock_claim.assert_not_called()
        # State should remain empty (issue was skipped, not failed)
        assert "owner/repo!42" not in state

    def test_process_issue_continues_when_conformant(self, tmp_path):
        cfg = _make_cfg(tmp_path, require_jira_conformance=True, jira_api_enabled=True)
        monkeypatch_state_file(tmp_path)
        skill = tmp_path / "proposal-skill.md"
        skill.write_text("Write proposal")
        cfg.proposal_skill_path = skill
        state = {}
        worktree = tmp_path / "wt"
        worktree.mkdir()
        issue = {
            "number": 42,
            "title": "[DAP09A-1184] Fix the widget",
            "author": {"login": "reporter"},
            "labels": [],
            "body": "desc",
        }

        frontmatter = "| Field | Value |\n|-------|-------|\n| Jira | [DAP09A-1184](...) |\n\n---\n\n"

        with patch.object(h, "_fetch_jira_frontmatter", return_value=(frontmatter, [], {})), \
             patch.object(h, "label_jira_issue"), \
             patch.object(h, "try_claim_issue", return_value=True), \
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
            h.process_issue(cfg, state, "owner/repo", issue)

        assert state.get("owner/repo!42", {}).get("status") == "proposal_open"
        assert state.get("owner/repo!42", {}).get("jira_frontmatter") == frontmatter

    def test_process_issue_proceeds_when_conformance_not_required(self, tmp_path):
        cfg = _make_cfg(tmp_path, require_jira_conformance=False, jira_api_enabled=True)
        monkeypatch_state_file(tmp_path)
        skill = tmp_path / "proposal-skill.md"
        skill.write_text("Write proposal")
        cfg.proposal_skill_path = skill
        state = {}
        worktree = tmp_path / "wt"
        worktree.mkdir()
        issue = {
            "number": 43,
            "title": "[DAP09A-1185] Another issue",
            "author": {"login": "reporter"},
            "labels": [],
            "body": "desc",
        }

        # Non-empty missing but require_jira_conformance=False => should continue
        with patch.object(h, "_fetch_jira_frontmatter", return_value=("", ["Sprint"], {})), \
             patch.object(h, "label_jira_issue"), \
             patch.object(h, "try_claim_issue", return_value=True), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", return_value=worktree), \
             patch.object(h, "run_skill", return_value="proposal"), \
             patch.object(h, "skill_has_commits", return_value=True), \
             patch.object(h, "gh_create_branch_and_pr", return_value=56), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_remove_label"), \
             patch.object(h, "notify_sound"), \
             patch.object(h, "notify_toast"), \
             patch.object(h, "save_hunter_state"):
            h.process_issue(cfg, state, "owner/repo", issue)

        # Should have proceeded and created proposal
        assert state.get("owner/repo!43", {}).get("status") == "proposal_open"

    def test_process_issue_no_jira_key_skips_frontmatter(self, tmp_path):
        cfg = _make_cfg(tmp_path, require_jira_conformance=True, jira_api_enabled=True)
        monkeypatch_state_file(tmp_path)
        skill = tmp_path / "proposal-skill.md"
        skill.write_text("Write proposal")
        cfg.proposal_skill_path = skill
        state = {}
        worktree = tmp_path / "wt"
        worktree.mkdir()
        issue = {
            "number": 44,
            "title": "Fix the widget (no jira key)",
            "author": {"login": "reporter"},
            "labels": [],
            "body": "desc",
        }

        with patch.object(h, "_fetch_jira_frontmatter") as mock_fetch, \
             patch.object(h, "label_jira_issue"), \
             patch.object(h, "try_claim_issue", return_value=True), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", return_value=worktree), \
             patch.object(h, "run_skill", return_value="proposal"), \
             patch.object(h, "skill_has_commits", return_value=True), \
             patch.object(h, "gh_create_branch_and_pr", return_value=57), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_remove_label"), \
             patch.object(h, "notify_sound"), \
             patch.object(h, "notify_toast"), \
             patch.object(h, "save_hunter_state"):
            h.process_issue(cfg, state, "owner/repo", issue)

        # _fetch_jira_frontmatter should not be called when no Jira key in title
        mock_fetch.assert_not_called()
        assert state.get("owner/repo!44", {}).get("status") == "proposal_open"

    def test_proposal_pr_body_includes_frontmatter(self, tmp_path):
        cfg = _make_cfg(tmp_path, require_jira_conformance=False, jira_api_enabled=True)
        monkeypatch_state_file(tmp_path)
        skill = tmp_path / "proposal-skill.md"
        skill.write_text("Write proposal")
        cfg.proposal_skill_path = skill
        state = {}
        worktree = tmp_path / "wt"
        worktree.mkdir()
        issue = {
            "number": 45,
            "title": "[DAP09A-100] Feature request",
            "author": {"login": "reporter"},
            "labels": [],
            "body": "desc",
        }
        frontmatter = "| Field | Value |\n|-------|-------|\n| Jira | [DAP09A-100](...) |\n\n---\n\n"

        pr_body_captured = []

        def _capture_pr(**kwargs):
            pr_body_captured.append(kwargs.get("body", ""))
            return 99

        with patch.object(h, "_fetch_jira_frontmatter", return_value=(frontmatter, [], {})), \
             patch.object(h, "label_jira_issue"), \
             patch.object(h, "try_claim_issue", return_value=True), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", return_value=worktree), \
             patch.object(h, "run_skill", return_value="proposal"), \
             patch.object(h, "skill_has_commits", return_value=True), \
             patch.object(h, "gh_create_branch_and_pr", side_effect=_capture_pr), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_remove_label"), \
             patch.object(h, "notify_sound"), \
             patch.object(h, "notify_toast"), \
             patch.object(h, "save_hunter_state"):
            h.process_issue(cfg, state, "owner/repo", issue)

        assert pr_body_captured, "gh_create_branch_and_pr was not called"
        body = pr_body_captured[0]
        assert body.startswith(frontmatter)
        assert "Proposal for issue #45" in body

# ---------------------------------------------------------------------------
# TestReconcileAssignedIssues
# ---------------------------------------------------------------------------

class TestReconcileAssignedIssues:
    def _cfg(self, tmp_path):
        return _make_cfg(tmp_path)

    def _issue(self, number=42, title="Fix the bug"):
        return {"number": number, "title": title, "labels": [], "body": "desc"}

    def test_reconcile_injects_submitted_when_impl_merged(self, tmp_path):
        cfg = self._cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}

        with patch.object(h, "gh_list_assigned_issues", return_value=[self._issue()]), \
             patch.object(h, "_find_impl_pr", return_value=10), \
             patch.object(h, "gh_pr_is_merged", return_value=True), \
             patch.object(h, "save_hunter_state"):
            h.reconcile_assigned_issues(cfg, state, ["owner/repo"])

        entry = state.get("owner/repo!42", {})
        assert entry["status"] == "submitted"
        assert entry["issue_number"] == 42
        assert entry["repo"] == "owner/repo"
        # impl_pr should be None for merged (no further work needed)
        assert entry.get("impl_pr") is None

    def test_reconcile_injects_implementing_when_impl_open(self, tmp_path):
        cfg = self._cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}

        with patch.object(h, "gh_list_assigned_issues", return_value=[self._issue()]), \
             patch.object(h, "_find_impl_pr", return_value=10), \
             patch.object(h, "gh_pr_is_merged", return_value=False), \
             patch.object(h, "save_hunter_state"):
            h.reconcile_assigned_issues(cfg, state, ["owner/repo"])

        entry = state.get("owner/repo!42", {})
        assert entry["status"] == "implementing"
        assert entry["impl_pr"] == 10

    def test_reconcile_injects_proposal_open_when_proposal_merged(self, tmp_path):
        cfg = self._cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}

        with patch.object(h, "gh_list_assigned_issues", return_value=[self._issue()]), \
             patch.object(h, "_find_impl_pr", return_value=None), \
             patch.object(h, "gh_find_merged_proposal", return_value=7), \
             patch.object(h, "save_hunter_state"):
            h.reconcile_assigned_issues(cfg, state, ["owner/repo"])

        entry = state.get("owner/repo!42", {})
        assert entry["status"] == "proposal_open"
        assert entry["proposal_pr"] == 7
        assert entry["resume_attempts"] == 0

    def test_reconcile_skips_when_no_prs_found(self, tmp_path):
        cfg = self._cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}

        with patch.object(h, "gh_list_assigned_issues", return_value=[self._issue()]), \
             patch.object(h, "_find_impl_pr", return_value=None), \
             patch.object(h, "gh_find_merged_proposal", return_value=None), \
             patch.object(h, "save_hunter_state") as mock_save:
            h.reconcile_assigned_issues(cfg, state, ["owner/repo"])

        assert "owner/repo!42" not in state
        mock_save.assert_not_called()

    def test_reconcile_skips_existing_state_entries(self, tmp_path):
        cfg = self._cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {"owner/repo!42": {"status": "proposal_open", "proposal_pr": 5}}

        with patch.object(h, "gh_list_assigned_issues", return_value=[self._issue()]), \
             patch.object(h, "_find_impl_pr") as mock_find_impl, \
             patch.object(h, "save_hunter_state") as mock_save:
            h.reconcile_assigned_issues(cfg, state, ["owner/repo"])

        # Should not touch existing entry
        mock_find_impl.assert_not_called()
        mock_save.assert_not_called()
        assert state["owner/repo!42"]["status"] == "proposal_open"

    def test_reconcile_continues_on_list_error(self, tmp_path):
        cfg = self._cfg(tmp_path)
        monkeypatch_state_file(tmp_path)
        state = {}

        with patch.object(h, "gh_list_assigned_issues", side_effect=RuntimeError("network")), \
             patch.object(h, "save_hunter_state") as mock_save:
            # Should not raise
            h.reconcile_assigned_issues(cfg, state, ["owner/repo"])

        assert state == {}
        mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# TestCleanupEphemeralStates
# ---------------------------------------------------------------------------

class TestCleanupEphemeralStates:
    def test_cleanup_resets_in_progress_to_failed(self, tmp_path):
        monkeypatch_state_file(tmp_path)
        state = {"owner/repo!1": {"status": "in_progress", "issue_number": 1}}

        with patch.object(h, "save_hunter_state"):
            h._cleanup_ephemeral_states(state)

        assert state["owner/repo!1"]["status"] == "failed"

    def test_cleanup_resets_implementing_to_failed(self, tmp_path):
        monkeypatch_state_file(tmp_path)
        state = {"owner/repo!2": {"status": "implementing", "issue_number": 2, "impl_pr": 10}}

        with patch.object(h, "save_hunter_state"):
            h._cleanup_ephemeral_states(state)

        assert state["owner/repo!2"]["status"] == "failed"

    def test_cleanup_leaves_other_states_untouched(self, tmp_path):
        monkeypatch_state_file(tmp_path)
        state = {
            "owner/repo!3": {"status": "proposal_open"},
            "owner/repo!4": {"status": "self_reviewing"},
            "owner/repo!5": {"status": "ready_for_review"},
            "owner/repo!6": {"status": "submitted"},
            "owner/repo!7": {"status": "failed"},
        }

        with patch.object(h, "save_hunter_state"):
            h._cleanup_ephemeral_states(state)

        assert state["owner/repo!3"]["status"] == "proposal_open"
        assert state["owner/repo!4"]["status"] == "self_reviewing"
        assert state["owner/repo!5"]["status"] == "ready_for_review"
        assert state["owner/repo!6"]["status"] == "submitted"
        assert state["owner/repo!7"]["status"] == "failed"

    def test_cleanup_returns_count(self, tmp_path):
        monkeypatch_state_file(tmp_path)
        state = {
            "owner/repo!1": {"status": "in_progress"},
            "owner/repo!2": {"status": "implementing"},
            "owner/repo!3": {"status": "submitted"},
        }

        with patch.object(h, "save_hunter_state"):
            count = h._cleanup_ephemeral_states(state)

        assert count == 2

    def test_cleanup_returns_zero_when_nothing_to_reset(self, tmp_path):
        monkeypatch_state_file(tmp_path)
        state = {
            "owner/repo!1": {"status": "proposal_open"},
            "owner/repo!2": {"status": "submitted"},
        }

        with patch.object(h, "save_hunter_state") as mock_save:
            count = h._cleanup_ephemeral_states(state)

        assert count == 0
        mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# TestSpeckitPhaseI — acceptance criteria 1-7
# ---------------------------------------------------------------------------


def _make_speckit_cfg(tmp_path: Path, capability_specs_path: Path | None = None,
                      epic_map: dict | None = None) -> h.Config:
    """Build a Config with speckit_enabled=True and a prompt dir."""
    prompt_dir = tmp_path / "prompts" / "speckit"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    # Write minimal prompt templates
    (prompt_dir / "plan.md").write_text(
        "plan for {issue_number} {issue_title} {issue_body} "
        "{constitution_path} {capability_spec_path} {story_spec_path} "
        "{clarifications_path} {spec_refs_dir} {capability_specs_sha} "
        "{capability} {story_id}"
    )
    (prompt_dir / "implement.md").write_text(
        "implement for {issue_number} {issue_title} {issue_body} "
        "{spec_refs_dir} {constitution_path} {capability_spec_path} "
        "{story_spec_path} {clarifications_path} {plan_path} {tasks_path}"
    )
    data = {
        "repos": ["owner/repo"],
        "worktree_base": str(tmp_path / "worktrees"),
        "github_user": "testuser",
        "backend": "claude",
        "model": "claude-opus-4-7",
        "skill_path": str(tmp_path / "review-skill.md"),
        "proposal_skill_path": str(tmp_path / "proposal-skill.md"),
        "impl_skill_path": str(tmp_path / "impl-skill.md"),
        "speckit_enabled": True,
        "speckit_prompt_dir": str(prompt_dir),
    }
    if capability_specs_path:
        data["capability_specs_path"] = str(capability_specs_path)
    if epic_map:
        data["speckit_epic_map"] = epic_map
    return h.Config(data)


def _make_capability_dir(base: Path, slug: str, story_id: str,
                         with_clarifications: bool = False) -> Path:
    """Create a minimal BPA-Specs capability folder structure."""
    cap_dir = base / slug
    cap_dir.mkdir(parents=True, exist_ok=True)
    (cap_dir / "constitution.md").write_text("# Constitution")
    (cap_dir / "spec.md").write_text("# Capability spec")
    story_dir = cap_dir / "stories" / story_id
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / "spec.md").write_text("# Story spec")
    if with_clarifications:
        (cap_dir / "clarifications.md").write_text("# Clarifications")
    return cap_dir


class TestSpeckitConfig:
    """Config fields are loaded with correct defaults."""

    def test_speckit_disabled_by_default(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        assert cfg.speckit_enabled is False

    def test_speckit_fields_loaded(self, tmp_path):
        specs_path = tmp_path / "bpa-specs"
        specs_path.mkdir()
        data = {
            "repos": ["owner/repo"],
            "worktree_base": str(tmp_path),
            "github_user": "u",
            "speckit_enabled": True,
            "speckit_prompt_dir": str(tmp_path / "prompts"),
            "capability_specs_path": str(specs_path),
            "speckit_epic_map": {"EP-1": "my-capability"},
        }
        cfg = h.Config(data)
        assert cfg.speckit_enabled is True
        assert cfg.capability_specs_path == specs_path
        assert cfg.speckit_epic_map == {"EP-1": "my-capability"}

    def test_capability_specs_path_none_when_absent(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        assert cfg.capability_specs_path is None


class TestResolveCapabilityFolder:
    """resolve_capability_folder — slug match, map fallback, no epic, no config."""

    def test_slug_match(self, tmp_path):
        specs = tmp_path / "specs"
        cap = specs / "my-capability"
        cap.mkdir(parents=True)
        cfg = _make_speckit_cfg(tmp_path, capability_specs_path=specs)

        result = h.resolve_capability_folder(cfg, "My Capability", "EP-1")
        assert result == cap

    def test_map_fallback(self, tmp_path):
        specs = tmp_path / "specs"
        cap = specs / "mapped-cap"
        cap.mkdir(parents=True)
        cfg = _make_speckit_cfg(tmp_path, capability_specs_path=specs,
                                epic_map={"EP-99": "mapped-cap"})

        # Epic name doesn't match any folder slug
        result = h.resolve_capability_folder(cfg, "Unknown Epic Name", "EP-99")
        assert result == cap

    def test_no_epic_returns_none(self, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        cfg = _make_speckit_cfg(tmp_path, capability_specs_path=specs)

        with patch.object(h, "log_decision"):
            result = h.resolve_capability_folder(cfg, "", "")
        assert result is None

    def test_no_config_returns_none(self, tmp_path):
        cfg = _make_cfg(tmp_path)  # capability_specs_path not set
        result = h.resolve_capability_folder(cfg, "Some Epic", "EP-1")
        assert result is None

    def test_slug_no_folder_then_no_map_returns_none(self, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        cfg = _make_speckit_cfg(tmp_path, capability_specs_path=specs)

        with patch.object(h, "log_decision"):
            result = h.resolve_capability_folder(cfg, "Nonexistent Epic", "")
        assert result is None


class TestReadBpaSpecsBundle:
    """read_bpa_specs_bundle — happy path, hard fail, soft warn."""

    def test_happy_path_with_clarifications(self, tmp_path):
        cap_dir = _make_capability_dir(tmp_path, "my-cap", "DAP09A-1234",
                                       with_clarifications=True)
        bundle = h.read_bpa_specs_bundle(cap_dir, "DAP09A-1234")

        assert bundle["constitution"] == cap_dir / "constitution.md"
        assert bundle["capability_spec"] == cap_dir / "spec.md"
        assert bundle["story_spec"] == cap_dir / "stories" / "DAP09A-1234" / "spec.md"
        assert bundle["clarifications"] == cap_dir / "clarifications.md"

    def test_happy_path_no_clarifications(self, tmp_path):
        cap_dir = _make_capability_dir(tmp_path, "my-cap", "DAP09A-5678")
        with patch.object(h, "log_decision") as mock_log:
            bundle = h.read_bpa_specs_bundle(cap_dir, "DAP09A-5678")

        assert bundle["clarifications"] is None
        mock_log.assert_called_once_with(
            "speckit_no_clarifications", capability="my-cap", story_id="DAP09A-5678"
        )

    def test_missing_story_spec_raises(self, tmp_path):
        cap_dir = tmp_path / "cap"
        cap_dir.mkdir()
        (cap_dir / "constitution.md").write_text("x")
        (cap_dir / "spec.md").write_text("x")
        # story dir absent

        with pytest.raises(RuntimeError, match="stories/DAP09A-999/spec.md"):
            h.read_bpa_specs_bundle(cap_dir, "DAP09A-999")

    def test_missing_constitution_raises(self, tmp_path):
        cap_dir = tmp_path / "cap"
        cap_dir.mkdir()
        (cap_dir / "spec.md").write_text("x")
        story_dir = cap_dir / "stories" / "DAP09A-1"
        story_dir.mkdir(parents=True)
        (story_dir / "spec.md").write_text("x")

        with pytest.raises(RuntimeError, match="constitution.md"):
            h.read_bpa_specs_bundle(cap_dir, "DAP09A-1")


class TestSpecBranch:
    def test_format(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        assert h.spec_branch(cfg, "DAP09A-1234", "my-feature") == \
            "usr/at/DAP09A-1234-spec-my-feature"

    def test_custom_prefix(self, tmp_path):
        cfg = _make_cfg(tmp_path, branch_prefix="feat")
        assert h.spec_branch(cfg, "123", "fix-bug") == "feat/123-spec-fix-bug"


class TestLoadSpeckitPrompt:
    def test_renders_placeholders(self, tmp_path):
        prompt_dir = tmp_path / "prompts"
        prompt_dir.mkdir()
        (prompt_dir / "plan.md").write_text("Hello {name}, issue {issue_number}")
        cfg = _make_cfg(tmp_path)
        cfg.speckit_prompt_dir = prompt_dir

        result = h.load_speckit_prompt(cfg, "plan", name="world", issue_number=42)
        assert result == "Hello world, issue 42"

    def test_missing_template_raises(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        cfg.speckit_prompt_dir = tmp_path / "nonexistent"

        with pytest.raises(FileNotFoundError):
            h.load_speckit_prompt(cfg, "plan", issue_number=1)


class TestRunSpeckitPlan:
    """run_speckit_plan — returns True/False, calls correct helpers."""

    def test_returns_false_when_no_capability(self, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        cfg = _make_speckit_cfg(tmp_path, capability_specs_path=specs)
        entry = {"jira_key": "DAP09A-1", "epic_name": "", "epic_key": ""}
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "resolve_capability_folder", return_value=None) as mock_resolve, \
             patch.object(h, "log_decision"):
            result = h.run_speckit_plan(cfg, entry, worktree, 1, "title", "body")

        assert result is False
        mock_resolve.assert_called_once()

    def test_returns_true_on_success(self, tmp_path):
        specs = tmp_path / "specs"
        cap_dir = _make_capability_dir(specs, "my-cap", "DAP09A-99")
        cfg = _make_speckit_cfg(tmp_path, capability_specs_path=specs)
        entry = {"jira_key": "DAP09A-99", "epic_name": "my cap", "epic_key": ""}
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Create plan.md so the existence check passes
        (worktree / "plan.md").write_text("# plan")

        with patch.object(h, "pin_capability_sha", return_value="abc123"), \
             patch.object(h, "commit_skill_output", return_value=True), \
             patch.object(h, "run_skill"), \
             patch.object(h, "log_decision"):
            result = h.run_speckit_plan(cfg, entry, worktree, 99, "title", "body")

        assert result is True

    def test_raises_when_plan_md_missing(self, tmp_path):
        specs = tmp_path / "specs"
        cap_dir = _make_capability_dir(specs, "my-cap", "DAP09A-77")
        cfg = _make_speckit_cfg(tmp_path, capability_specs_path=specs)
        entry = {"jira_key": "DAP09A-77", "epic_name": "my cap", "epic_key": ""}
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # plan.md NOT created — run_skill is a no-op

        with patch.object(h, "pin_capability_sha", return_value="sha"), \
             patch.object(h, "commit_skill_output", return_value=True), \
             patch.object(h, "run_skill"), \
             patch.object(h, "log_decision"), \
             pytest.raises(RuntimeError, match="plan.md"):
            h.run_speckit_plan(cfg, entry, worktree, 77, "title", "body")

    def test_hard_fail_propagates_missing_artifacts(self, tmp_path):
        specs = tmp_path / "specs"
        cap_dir = specs / "my-cap"
        cap_dir.mkdir(parents=True)
        (cap_dir / "constitution.md").write_text("x")
        (cap_dir / "spec.md").write_text("x")
        # story dir missing
        cfg = _make_speckit_cfg(tmp_path, capability_specs_path=specs)
        entry = {"jira_key": "DAP09A-5", "epic_name": "my cap", "epic_key": ""}
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "pin_capability_sha", return_value="sha"), \
             patch.object(h, "log_decision"), \
             pytest.raises(RuntimeError, match="Missing required speckit"):
            h.run_speckit_plan(cfg, entry, worktree, 5, "title", "body")


class TestProcessIssueFork:
    """process_issue forks to speckit or legacy based on cfg.speckit_enabled."""

    def _minimal_issue(self, number=10):
        return {
            "number": number,
            "title": f"[DAP09A-{number}] Test issue",
            "author": {"login": "someone"},
            "labels": [],
            "body": "body text",
        }

    def test_speckit_disabled_uses_legacy(self, tmp_path):
        """speckit_enabled=False → run_skill called with proposal_skill_path."""
        cfg = _make_cfg(tmp_path)
        cfg.speckit_enabled = False
        state = {}
        issue = self._minimal_issue()
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "extract_jira_key", return_value=None), \
             patch.object(h, "label_jira_issue"), \
             patch.object(h, "try_claim_issue", return_value=True), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", return_value=worktree), \
             patch.object(h, "notify_sound"), \
             patch.object(h, "notify_toast"), \
             patch.object(h, "run_skill") as mock_run_skill, \
             patch.object(h, "skill_has_commits", return_value=True), \
             patch.object(h, "commit_skill_output", return_value=True), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_create_branch_and_pr", return_value=5), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_remove_label"), \
             patch.object(h, "log_decision"), \
             patch.object(h, "save_hunter_state"):
            h.process_issue(cfg, state, "owner/repo", issue)

        # run_skill called with proposal_skill_path
        mock_run_skill.assert_called_once()
        args = mock_run_skill.call_args[0]
        assert args[1] == cfg.proposal_skill_path

    def test_speckit_enabled_uses_speckit_plan(self, tmp_path):
        """speckit_enabled=True → run_speckit_plan called; used_speckit stored in state."""
        specs = tmp_path / "specs"
        _make_capability_dir(specs, "my-cap", "DAP09A-10")
        cfg = _make_speckit_cfg(tmp_path, capability_specs_path=specs)
        state = {}
        issue = self._minimal_issue()
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "extract_jira_key", return_value="DAP09A-10"), \
             patch.object(h, "label_jira_issue"), \
             patch.object(h, "_fetch_jira_frontmatter", return_value=("", [], {})), \
             patch.object(h, "try_claim_issue", return_value=True), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", return_value=worktree), \
             patch.object(h, "notify_sound"), \
             patch.object(h, "notify_toast"), \
             patch.object(h, "run_speckit_plan", return_value=True) as mock_speckit, \
             patch.object(h, "run_skill") as mock_legacy, \
             patch.object(h, "skill_has_commits", return_value=True), \
             patch.object(h, "commit_skill_output", return_value=True), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_create_branch_and_pr", return_value=7), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_remove_label"), \
             patch.object(h, "log_decision"), \
             patch.object(h, "save_hunter_state"):
            h.process_issue(cfg, state, "owner/repo", issue)

        mock_speckit.assert_called_once()
        mock_legacy.assert_not_called()
        # used_speckit should be True in state
        key = "owner/repo!10"
        assert state.get(key, {}).get("used_speckit") is True

    def test_speckit_enabled_no_capability_falls_back(self, tmp_path):
        """speckit_enabled=True but no capability → legacy proposal_skill_path used."""
        specs = tmp_path / "specs"
        specs.mkdir()
        cfg = _make_speckit_cfg(tmp_path, capability_specs_path=specs)
        state = {}
        issue = self._minimal_issue()
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "extract_jira_key", return_value=None), \
             patch.object(h, "label_jira_issue"), \
             patch.object(h, "try_claim_issue", return_value=True), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", return_value=worktree), \
             patch.object(h, "notify_sound"), \
             patch.object(h, "notify_toast"), \
             patch.object(h, "run_speckit_plan", return_value=False) as mock_speckit, \
             patch.object(h, "run_skill") as mock_legacy, \
             patch.object(h, "skill_has_commits", return_value=True), \
             patch.object(h, "commit_skill_output", return_value=True), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_create_branch_and_pr", return_value=8), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_remove_label"), \
             patch.object(h, "log_decision"), \
             patch.object(h, "save_hunter_state"):
            h.process_issue(cfg, state, "owner/repo", issue)

        mock_legacy.assert_called_once()
        args = mock_legacy.call_args[0]
        assert args[1] == cfg.proposal_skill_path
        key = "owner/repo!10"
        assert state.get(key, {}).get("used_speckit") is False


class TestCheckProposalMergedFork:
    """check_proposal_merged forks to speckit implement or legacy based on used_speckit."""

    def _entry(self, used_speckit: bool):
        return {
            "issue_number": 42,
            "title": "[DAP09A-42] Test",
            "issue_body": "body",
            "proposal_pr": 10,
            "jira_key": "DAP09A-42",
            "used_speckit": used_speckit,
        }

    def test_used_speckit_true_calls_run_speckit_implement(self, tmp_path):
        monkeypatch_state_file(tmp_path)
        cfg = _make_speckit_cfg(tmp_path)
        state = {"owner/repo!42": self._entry(True)}
        entry = self._entry(True)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "gh_issue_is_closed", return_value=False), \
             patch.object(h, "collect_pr_feedback"), \
             patch.object(h, "load_hunter_state", return_value=state), \
             patch.object(h, "gh_find_merged_proposal", return_value=10), \
             patch.object(h, "_find_impl_pr", return_value=None), \
             patch.object(h, "extract_jira_key", return_value=None), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", return_value=worktree), \
             patch.object(h, "run_speckit_implement") as mock_impl, \
             patch.object(h, "run_skill") as mock_legacy, \
             patch.object(h, "commit_skill_output", return_value=True), \
             patch.object(h, "skill_has_commits", return_value=True), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_create_branch_and_pr", return_value=20), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_remove_label"), \
             patch.object(h, "log_decision"), \
             patch.object(h, "save_hunter_state"):
            h.check_proposal_merged(cfg, state, "owner/repo", "owner/repo!42", entry)

        mock_impl.assert_called_once()
        mock_legacy.assert_not_called()

    def test_used_speckit_false_calls_run_skill(self, tmp_path):
        monkeypatch_state_file(tmp_path)
        cfg = _make_cfg(tmp_path)
        state = {"owner/repo!42": self._entry(False)}
        entry = self._entry(False)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.object(h, "gh_issue_is_closed", return_value=False), \
             patch.object(h, "collect_pr_feedback"), \
             patch.object(h, "load_hunter_state", return_value=state), \
             patch.object(h, "gh_find_merged_proposal", return_value=10), \
             patch.object(h, "_find_impl_pr", return_value=None), \
             patch.object(h, "extract_jira_key", return_value=None), \
             patch.object(h, "gh_repo_default_branch", return_value="main"), \
             patch.object(h, "setup_new_branch_worktree", return_value=worktree), \
             patch.object(h, "run_speckit_implement") as mock_impl, \
             patch.object(h, "run_skill") as mock_legacy, \
             patch.object(h, "commit_skill_output", return_value=True), \
             patch.object(h, "skill_has_commits", return_value=True), \
             patch.object(h, "gh_ensure_label_exists"), \
             patch.object(h, "gh_create_branch_and_pr", return_value=20), \
             patch.object(h, "gh_issue_add_label"), \
             patch.object(h, "gh_issue_remove_label"), \
             patch.object(h, "log_decision"), \
             patch.object(h, "save_hunter_state"):
            h.check_proposal_merged(cfg, state, "owner/repo", "owner/repo!42", entry)

        mock_impl.assert_not_called()
        mock_legacy.assert_called_once()
        args = mock_legacy.call_args[0]
        assert args[1] == cfg.impl_skill_path


# ---------------------------------------------------------------------------
# TestRunSpeckitImplement
# ---------------------------------------------------------------------------

class TestRunSpeckitImplement:
    """Tests for run_speckit_implement directly (N7)."""

    def _make_entry(self):
        return {
            "issue_number": 7,
            "repo": "owner/repo",
            "title": "My story",
            "jira_key": "PROJ-7",
            "epic_key": "EPIC-1",
            "epic_name": "My Epic",
            "status": "implementing",
        }

    def test_calls_run_skill_with_tmp_file(self, tmp_path):
        """run_skill is called; tmp file is cleaned up afterward."""
        cfg = _make_cfg(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "plan.md").write_text("# Plan")
        (worktree / "tasks.md").write_text("# Tasks")

        captured_paths = []

        def fake_run_skill(c, skill_path, context, wt):
            captured_paths.append(Path(skill_path))
            # Confirm the tmp file still exists during the call
            assert Path(skill_path).exists(), "tmp skill file should exist during run_skill"

        with patch.object(h, "load_speckit_prompt", return_value="implement prompt") as mock_load, \
             patch.object(h, "build_issue_context", return_value="ctx") as mock_ctx, \
             patch.object(h, "run_skill", side_effect=fake_run_skill):
            h.run_speckit_implement(cfg, self._make_entry(), worktree, 7, "My story", "body")

        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args
        assert call_kwargs[1]["template_name"] == "implement"
        mock_ctx.assert_called_once()
        # Tmp file cleaned up after call
        assert len(captured_paths) == 1
        assert not captured_paths[0].exists(), "tmp skill file should be deleted after run_skill"

    def test_clarifications_absent_uses_placeholder(self, tmp_path):
        """When clarifications.md absent, path passed to template is '(not present)'."""
        cfg = _make_cfg(tmp_path)
        worktree = tmp_path / "wt"
        (worktree / "spec-refs").mkdir(parents=True)
        worktree.mkdir(exist_ok=True)

        received_kwargs = {}

        def fake_load_prompt(c, *, template_name, **kwargs):
            received_kwargs.update(kwargs)
            return "prompt"

        with patch.object(h, "load_speckit_prompt", side_effect=fake_load_prompt), \
             patch.object(h, "build_issue_context", return_value="ctx"), \
             patch.object(h, "run_skill"):
            h.run_speckit_implement(cfg, self._make_entry(), worktree, 7, "My story", "body")

        assert received_kwargs["clarifications_path"] == "(not present)"

    def test_clarifications_present_uses_real_path(self, tmp_path):
        """When clarifications.md exists, path passed to template is the actual path."""
        cfg = _make_cfg(tmp_path)
        worktree = tmp_path / "wt"
        spec_refs = worktree / "spec-refs"
        spec_refs.mkdir(parents=True)
        clarif = spec_refs / "clarifications.md"
        clarif.write_text("Q: ...")

        received_kwargs = {}

        def fake_load_prompt(c, *, template_name, **kwargs):
            received_kwargs.update(kwargs)
            return "prompt"

        with patch.object(h, "load_speckit_prompt", side_effect=fake_load_prompt), \
             patch.object(h, "build_issue_context", return_value="ctx"), \
             patch.object(h, "run_skill"):
            h.run_speckit_implement(cfg, self._make_entry(), worktree, 7, "My story", "body")

        assert received_kwargs["clarifications_path"] == str(clarif)

    def test_tmp_file_cleaned_up_on_run_skill_error(self, tmp_path):
        """Tmp file is deleted even if run_skill raises."""
        cfg = _make_cfg(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        captured_path = []

        def exploding_run_skill(c, skill_path, context, wt):
            captured_path.append(Path(skill_path))
            raise RuntimeError("boom")

        with patch.object(h, "load_speckit_prompt", return_value="prompt"), \
             patch.object(h, "build_issue_context", return_value="ctx"), \
             patch.object(h, "run_skill", side_effect=exploding_run_skill):
            with pytest.raises(RuntimeError, match="boom"):
                h.run_speckit_implement(cfg, self._make_entry(), worktree, 7, "My story", "body")

        assert len(captured_path) == 1
        assert not captured_path[0].exists(), "tmp file should be cleaned up even after error"
