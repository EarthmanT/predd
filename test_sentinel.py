"""Tests for sentinel.py — post-CI review of hunter-created PRs."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Import sentinel module
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent))

_spec = importlib.util.spec_from_file_location("sentinel", Path(__file__).parent / "sentinel.py")
s = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s)

# Access the predd module that sentinel imported
_predd = s._predd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path, **overrides) -> s.Config:
    data = {
        "repos": ["owner/repo"],
        "worktree_base": str(tmp_path / "worktrees"),
        "github_user": "testuser",
        "backend": "claude",
        "model": "claude-opus-4-7",
        "skill_path": str(tmp_path / "review-skill.md"),
        "proposal_skill_path": str(tmp_path / "proposal-skill.md"),
        "impl_skill_path": str(tmp_path / "impl-skill.md"),
        "post_ci_skill_path": str(tmp_path / "post-ci-skill.md"),
    }
    data.update(overrides)
    return s.Config(data)


def _make_completed_run(conclusion="success") -> dict:
    return {"status": "completed", "conclusion": conclusion}


def _make_pending_run(status="in_progress") -> dict:
    return {"status": status, "conclusion": None}


# ---------------------------------------------------------------------------
# TestFingerprint
# ---------------------------------------------------------------------------

class TestFingerprint:
    def test_returns_16_chars(self):
        fp = s._fingerprint(42, "Fix flaky test", "workflow:run/123:test")
        assert len(fp) == 16

    def test_deterministic(self):
        fp1 = s._fingerprint(42, "Fix flaky test", "workflow:run/123:test")
        fp2 = s._fingerprint(42, "Fix flaky test", "workflow:run/123:test")
        assert fp1 == fp2

    def test_different_inputs_give_different_fingerprints(self):
        fp1 = s._fingerprint(42, "Fix flaky test", "workflow:run/123:test")
        fp2 = s._fingerprint(42, "Fix different test", "workflow:run/123:test")
        assert fp1 != fp2

    def test_source_pr_matters(self):
        fp1 = s._fingerprint(1, "title", "source")
        fp2 = s._fingerprint(2, "title", "source")
        assert fp1 != fp2

    def test_only_hex_chars(self):
        fp = s._fingerprint(1, "hello", "world")
        assert all(c in "0123456789abcdef" for c in fp)


# ---------------------------------------------------------------------------
# TestCiIsFinished
# ---------------------------------------------------------------------------

class TestCiIsFinished:
    def test_empty_runs_is_finished(self):
        assert s._ci_is_finished([]) is True

    def test_all_success_is_finished(self):
        runs = [_make_completed_run("success"), _make_completed_run("success")]
        assert s._ci_is_finished(runs) is True

    def test_one_in_progress_not_finished(self):
        runs = [_make_completed_run("success"), _make_pending_run("in_progress")]
        assert s._ci_is_finished(runs) is False

    def test_queued_not_finished(self):
        runs = [_make_pending_run("queued")]
        assert s._ci_is_finished(runs) is False

    def test_failure_is_terminal(self):
        runs = [_make_completed_run("failure")]
        assert s._ci_is_finished(runs) is True

    def test_cancelled_is_terminal(self):
        runs = [_make_completed_run("cancelled")]
        assert s._ci_is_finished(runs) is True

    def test_timed_out_is_terminal(self):
        runs = [_make_completed_run("timed_out")]
        assert s._ci_is_finished(runs) is True

    def test_action_required_is_terminal(self):
        runs = [_make_completed_run("action_required")]
        assert s._ci_is_finished(runs) is True

    def test_neutral_is_terminal(self):
        runs = [_make_completed_run("neutral")]
        assert s._ci_is_finished(runs) is True

    def test_skipped_is_terminal(self):
        runs = [_make_completed_run("skipped")]
        assert s._ci_is_finished(runs) is True

    def test_completed_but_no_conclusion_not_finished(self):
        runs = [{"status": "completed", "conclusion": None}]
        assert s._ci_is_finished(runs) is False

    def test_mixed_terminal_and_pending(self):
        runs = [_make_completed_run("success"), _make_pending_run("in_progress")]
        assert s._ci_is_finished(runs) is False


# ---------------------------------------------------------------------------
# TestAlreadyFiled
# ---------------------------------------------------------------------------

class TestAlreadyFiled:
    def test_returns_true_when_issues_found(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '[{"number": 10}]'

        with patch.object(s, "gh_run", return_value=mock_result):
            result = s._already_filed("owner/repo", "abc123", "testuser")

        assert result is True

    def test_returns_false_when_no_issues(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "[]"

        with patch.object(s, "gh_run", return_value=mock_result):
            result = s._already_filed("owner/repo", "abc123", "testuser")

        assert result is False

    def test_returns_false_on_gh_error(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch.object(s, "gh_run", return_value=mock_result):
            result = s._already_filed("owner/repo", "abc123", "testuser")

        assert result is False

    def test_returns_false_on_empty_stdout(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""

        with patch.object(s, "gh_run", return_value=mock_result):
            result = s._already_filed("owner/repo", "abc123", "testuser")

        assert result is False

    def test_passes_fingerprint_to_search(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "[]"

        with patch.object(s, "gh_run", return_value=mock_result) as mock_gh:
            s._already_filed("owner/repo", "deadbeef1234abcd", "testuser")

        call_args = mock_gh.call_args[0][0]
        assert any("deadbeef1234abcd" in str(a) for a in call_args)


# ---------------------------------------------------------------------------
# TestOpenAutoFiledCount
# ---------------------------------------------------------------------------

class TestOpenAutoFiledCount:
    def test_counts_correctly(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '[{"number": 1}, {"number": 2}, {"number": 3}]'

        with patch.object(s, "gh_run", return_value=mock_result):
            count = s._open_auto_filed_count("owner/repo", "testuser")

        assert count == 3

    def test_returns_zero_on_empty(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "[]"

        with patch.object(s, "gh_run", return_value=mock_result):
            count = s._open_auto_filed_count("owner/repo", "testuser")

        assert count == 0

    def test_returns_zero_on_error(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch.object(s, "gh_run", return_value=mock_result):
            count = s._open_auto_filed_count("owner/repo", "testuser")

        assert count == 0

    def test_uses_correct_label(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "[]"

        with patch.object(s, "gh_run", return_value=mock_result) as mock_gh:
            s._open_auto_filed_count("owner/repo", "myuser")

        call_args = mock_gh.call_args[0][0]
        assert "myuser:auto-filed" in call_args


# ---------------------------------------------------------------------------
# TestFileFinding
# ---------------------------------------------------------------------------

class TestFileFinding:
    def _finding(self, **overrides):
        base = {
            "title": "Add retry to flaky CSV test",
            "severity": "concern",
            "source": "workflow:run/123:test",
            "rationale": "Test fails intermittently under load.",
            "suggested_fix": "Wrap CSV parse in try/except with retry.",
        }
        base.update(overrides)
        return base

    def test_skips_when_already_filed(self, tmp_path):
        cfg = _make_cfg(tmp_path)

        with patch.object(s, "_already_filed", return_value=True) as mock_af, \
             patch.object(s, "gh_run") as mock_gh, \
             patch.object(s, "log_decision") as mock_log:
            result = s._file_finding(cfg, "owner/repo", 42, self._finding())

        assert result is None
        mock_gh.assert_not_called()
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "post_ci_finding_skipped"

    def test_skips_when_at_cap(self, tmp_path):
        cfg = _make_cfg(tmp_path, max_open_auto_issues=2)

        with patch.object(s, "_already_filed", return_value=False), \
             patch.object(s, "_open_auto_filed_count", return_value=2) as mock_count, \
             patch.object(s, "gh_run") as mock_gh, \
             patch.object(s, "log_decision") as mock_log:
            result = s._file_finding(cfg, "owner/repo", 42, self._finding())

        assert result is None
        mock_gh.assert_not_called()
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "post_ci_finding_deferred"

    def test_creates_issue_when_clear(self, tmp_path):
        cfg = _make_cfg(tmp_path, max_open_auto_issues=5)

        create_result = MagicMock()
        create_result.returncode = 0
        create_result.stdout = "https://github.com/owner/repo/issues/77\n"

        with patch.object(s, "_already_filed", return_value=False), \
             patch.object(s, "_open_auto_filed_count", return_value=0), \
             patch.object(s, "gh_run", return_value=create_result) as mock_gh, \
             patch.object(s, "log_decision") as mock_log:
            result = s._file_finding(cfg, "owner/repo", 42, self._finding())

        assert result == 77
        mock_gh.assert_called_once()
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "post_ci_finding_filed"

    def test_returns_none_when_gh_fails(self, tmp_path):
        cfg = _make_cfg(tmp_path)

        fail_result = MagicMock()
        fail_result.returncode = 1
        fail_result.stderr = "label not found"
        fail_result.stdout = ""

        with patch.object(s, "_already_filed", return_value=False), \
             patch.object(s, "_open_auto_filed_count", return_value=0), \
             patch.object(s, "gh_run", return_value=fail_result):
            result = s._file_finding(cfg, "owner/repo", 42, self._finding())

        assert result is None

    def test_assigns_to_user_when_auto_assign_enabled(self, tmp_path):
        cfg = _make_cfg(tmp_path, auto_assign_filed_issues=True)

        create_result = MagicMock()
        create_result.returncode = 0
        create_result.stdout = "https://github.com/owner/repo/issues/88\n"

        with patch.object(s, "_already_filed", return_value=False), \
             patch.object(s, "_open_auto_filed_count", return_value=0), \
             patch.object(s, "gh_run", return_value=create_result) as mock_gh:
            s._file_finding(cfg, "owner/repo", 42, self._finding())

        call_args = mock_gh.call_args[0][0]
        assert "--assignee" in call_args
        assert "testuser" in call_args

    def test_no_assignee_when_auto_assign_disabled(self, tmp_path):
        cfg = _make_cfg(tmp_path, auto_assign_filed_issues=False)

        create_result = MagicMock()
        create_result.returncode = 0
        create_result.stdout = "https://github.com/owner/repo/issues/89\n"

        with patch.object(s, "_already_filed", return_value=False), \
             patch.object(s, "_open_auto_filed_count", return_value=0), \
             patch.object(s, "gh_run", return_value=create_result) as mock_gh:
            s._file_finding(cfg, "owner/repo", 42, self._finding())

        call_args = mock_gh.call_args[0][0]
        assert "--assignee" not in call_args

    def test_body_contains_fingerprint_comment(self, tmp_path):
        cfg = _make_cfg(tmp_path)

        create_result = MagicMock()
        create_result.returncode = 0
        create_result.stdout = "https://github.com/owner/repo/issues/90\n"

        with patch.object(s, "_already_filed", return_value=False), \
             patch.object(s, "_open_auto_filed_count", return_value=0), \
             patch.object(s, "gh_run", return_value=create_result) as mock_gh:
            s._file_finding(cfg, "owner/repo", 42, self._finding())

        call_args = mock_gh.call_args[0][0]
        body_idx = call_args.index("--body") + 1
        body = call_args[body_idx]
        assert "sentinel-fingerprint:" in body
        assert "sentinel-source-pr: 42" in body

    def test_skips_finding_with_missing_title(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        finding = self._finding(title="")

        with patch.object(s, "gh_run") as mock_gh:
            result = s._file_finding(cfg, "owner/repo", 42, finding)

        assert result is None
        mock_gh.assert_not_called()


# ---------------------------------------------------------------------------
# TestParseFindings
# ---------------------------------------------------------------------------

class TestParseFindings:
    def test_parses_valid_findings_json(self):
        raw = json.dumps({
            "findings": [
                {
                    "title": "Fix flaky test",
                    "severity": "concern",
                    "source": "workflow:run/1:test",
                    "rationale": "It fails sometimes.",
                    "suggested_fix": "Add retry.",
                }
            ]
        })
        findings = s._parse_findings(raw)
        assert len(findings) == 1
        assert findings[0]["title"] == "Fix flaky test"

    def test_handles_markdown_fences(self):
        raw = '```json\n{"findings": [{"title": "t", "severity": "blocker", "source": "s", "rationale": "r", "suggested_fix": "f"}]}\n```'
        findings = s._parse_findings(raw)
        assert len(findings) == 1

    def test_returns_empty_list_on_invalid_json(self):
        findings = s._parse_findings("this is not json at all")
        assert findings == []

    def test_returns_empty_list_on_empty_findings(self):
        raw = '{"findings": []}'
        findings = s._parse_findings(raw)
        assert findings == []

    def test_handles_json_embedded_in_text(self):
        raw = 'Here is my analysis:\n\n{"findings": [{"title": "t", "severity": "nit", "source": "s", "rationale": "r", "suggested_fix": "f"}]}'
        findings = s._parse_findings(raw)
        assert len(findings) == 1

    def test_handles_malformed_json_gracefully(self):
        raw = '{"findings": [{"title": "incomplete'
        findings = s._parse_findings(raw)
        assert findings == []


# ---------------------------------------------------------------------------
# TestRunPostCiReview
# ---------------------------------------------------------------------------

class TestRunPostCiReview:
    def _pr_json(self, head_ref="usr/at/fix-123", conclusion=None, labels=None):
        return json.dumps({
            "number": 10,
            "title": "Fix the thing",
            "body": "Closes #99",
            "headRefName": head_ref,
            "headRefOid": "abc123",
            "labels": labels or [],
            "author": {"login": "testuser"},
        })

    def test_skips_if_already_post_ci_reviewed(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = {"owner/repo#10": {"status": "submitted", "post_ci_reviewed": True}}

        with patch.object(s, "gh_run") as mock_gh:
            s.run_post_ci_review(cfg, state, "owner/repo", 10)

        mock_gh.assert_not_called()

    def test_skips_if_not_hunter_pr(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = {"owner/repo#10": {"status": "submitted"}}

        pr_data = json.dumps({
            "number": 10,
            "title": "Stranger's PR",
            "body": "",
            "headRefName": "feature/some-feature",
            "headRefOid": "deadbeef",
            "labels": [],
            "author": {"login": "otheruser"},
        })

        check_result = MagicMock()
        check_result.returncode = 0
        check_result.stdout = pr_data

        with patch.object(s, "gh_run", return_value=check_result) as mock_gh, \
             patch.object(s, "_fetch_check_runs") as mock_ci:
            s.run_post_ci_review(cfg, state, "owner/repo", 10)

        mock_ci.assert_not_called()

    def test_skips_if_ci_not_finished(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = {"owner/repo#10": {"status": "submitted"}}

        pr_result = MagicMock()
        pr_result.returncode = 0
        pr_result.stdout = self._pr_json()

        with patch.object(s, "gh_run", return_value=pr_result), \
             patch.object(s, "_fetch_check_runs", return_value=[_make_pending_run("in_progress")]), \
             patch.object(s, "_run_review_skill") as mock_skill:
            s.run_post_ci_review(cfg, state, "owner/repo", 10)

        mock_skill.assert_not_called()

    def test_runs_skill_when_ci_finished(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = {"owner/repo#10": {"status": "submitted"}}

        pr_result = MagicMock()
        pr_result.returncode = 0
        pr_result.stdout = self._pr_json()

        diff_result = MagicMock()
        diff_result.returncode = 0
        diff_result.stdout = "diff --git a/foo.py b/foo.py"

        def mock_gh_run(args, check=True):
            if "pr" in args and "view" in args:
                return pr_result
            if "pr" in args and "diff" in args:
                return diff_result
            return MagicMock(returncode=0, stdout="[]")

        with patch.object(s, "gh_run", side_effect=mock_gh_run), \
             patch.object(s, "_fetch_check_runs", return_value=[_make_completed_run("success")]), \
             patch.object(s, "_fetch_workflow_logs", return_value=""), \
             patch.object(s, "_run_review_skill", return_value='{"findings": []}') as mock_skill, \
             patch.object(s, "save_state") as mock_save, \
             patch.object(s, "log_decision"):
            s.run_post_ci_review(cfg, state, "owner/repo", 10)

        mock_skill.assert_called_once()
        mock_save.assert_called_once()

    def test_marks_post_ci_reviewed_after_run(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = {"owner/repo#10": {"status": "submitted"}}

        pr_result = MagicMock()
        pr_result.returncode = 0
        pr_result.stdout = self._pr_json()

        diff_result = MagicMock()
        diff_result.returncode = 0
        diff_result.stdout = "diff content"

        def mock_gh_run(args, check=True):
            if "pr" in args and "view" in args:
                return pr_result
            if "pr" in args and "diff" in args:
                return diff_result
            return MagicMock(returncode=0, stdout="[]")

        saved_state = {}

        def capture_save(st):
            saved_state.update(st)

        with patch.object(s, "gh_run", side_effect=mock_gh_run), \
             patch.object(s, "_fetch_check_runs", return_value=[_make_completed_run("success")]), \
             patch.object(s, "_fetch_workflow_logs", return_value=""), \
             patch.object(s, "_run_review_skill", return_value='{"findings": []}'), \
             patch.object(s, "save_state", side_effect=capture_save), \
             patch.object(s, "log_decision"):
            s.run_post_ci_review(cfg, state, "owner/repo", 10)

        assert saved_state.get("owner/repo#10", {}).get("post_ci_reviewed") is True

    def test_nit_findings_are_not_filed(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = {"owner/repo#10": {"status": "submitted"}}

        pr_result = MagicMock()
        pr_result.returncode = 0
        pr_result.stdout = self._pr_json()

        diff_result = MagicMock()
        diff_result.returncode = 0
        diff_result.stdout = "diff content"

        nit_findings = json.dumps({
            "findings": [
                {
                    "title": "Rename variable",
                    "severity": "nit",
                    "source": "code:foo.py:10",
                    "rationale": "Style preference.",
                    "suggested_fix": "Rename x to count.",
                }
            ]
        })

        def mock_gh_run(args, check=True):
            if "pr" in args and "view" in args:
                return pr_result
            if "pr" in args and "diff" in args:
                return diff_result
            return MagicMock(returncode=0, stdout="[]")

        with patch.object(s, "gh_run", side_effect=mock_gh_run), \
             patch.object(s, "_fetch_check_runs", return_value=[_make_completed_run("success")]), \
             patch.object(s, "_fetch_workflow_logs", return_value=""), \
             patch.object(s, "_run_review_skill", return_value=nit_findings), \
             patch.object(s, "_file_finding") as mock_file, \
             patch.object(s, "save_state"), \
             patch.object(s, "log_decision"):
            s.run_post_ci_review(cfg, state, "owner/repo", 10)

        mock_file.assert_not_called()

    def test_blocker_and_concern_findings_are_filed(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = {"owner/repo#10": {"status": "submitted"}}

        pr_result = MagicMock()
        pr_result.returncode = 0
        pr_result.stdout = self._pr_json()

        diff_result = MagicMock()
        diff_result.returncode = 0
        diff_result.stdout = "diff content"

        findings_json = json.dumps({
            "findings": [
                {
                    "title": "SQL injection",
                    "severity": "blocker",
                    "source": "code:db.py:42",
                    "rationale": "Unsanitised input.",
                    "suggested_fix": "Use parameterised queries.",
                },
                {
                    "title": "Missing test coverage",
                    "severity": "concern",
                    "source": "code:parser.py:100",
                    "rationale": "Edge case not covered.",
                    "suggested_fix": "Add test for empty input.",
                },
            ]
        })

        def mock_gh_run(args, check=True):
            if "pr" in args and "view" in args:
                return pr_result
            if "pr" in args and "diff" in args:
                return diff_result
            return MagicMock(returncode=0, stdout="[]")

        file_call_count = []

        def mock_file_finding(cfg, repo, source_pr, finding):
            file_call_count.append(finding["title"])
            return 100 + len(file_call_count)

        with patch.object(s, "gh_run", side_effect=mock_gh_run), \
             patch.object(s, "_fetch_check_runs", return_value=[_make_completed_run("success")]), \
             patch.object(s, "_fetch_workflow_logs", return_value=""), \
             patch.object(s, "_run_review_skill", return_value=findings_json), \
             patch.object(s, "_file_finding", side_effect=mock_file_finding), \
             patch.object(s, "save_state"), \
             patch.object(s, "log_decision"):
            s.run_post_ci_review(cfg, state, "owner/repo", 10)

        assert len(file_call_count) == 2

    def test_skill_failure_marks_reviewed(self, tmp_path):
        """A skill crash should still set post_ci_reviewed=True to avoid infinite retries."""
        cfg = _make_cfg(tmp_path)
        state = {"owner/repo#10": {"status": "submitted"}}

        pr_result = MagicMock()
        pr_result.returncode = 0
        pr_result.stdout = self._pr_json()

        diff_result = MagicMock()
        diff_result.returncode = 0
        diff_result.stdout = "diff content"

        def mock_gh_run(args, check=True):
            if "pr" in args and "view" in args:
                return pr_result
            if "pr" in args and "diff" in args:
                return diff_result
            return MagicMock(returncode=0, stdout="[]")

        saved_state = {}

        def capture_save(st):
            saved_state.update(st)

        with patch.object(s, "gh_run", side_effect=mock_gh_run), \
             patch.object(s, "_fetch_check_runs", return_value=[_make_completed_run("success")]), \
             patch.object(s, "_fetch_workflow_logs", return_value=""), \
             patch.object(s, "_run_review_skill", side_effect=RuntimeError("skill exploded")), \
             patch.object(s, "save_state", side_effect=capture_save), \
             patch.object(s, "log_decision"):
            s.run_post_ci_review(cfg, state, "owner/repo", 10)

        assert saved_state.get("owner/repo#10", {}).get("post_ci_reviewed") is True


# ---------------------------------------------------------------------------
# TestIsHunterPr
# ---------------------------------------------------------------------------

class TestIsHunterPr:
    def _pr(self, head_ref="main", labels=None, author="otheruser"):
        return {
            "headRefName": head_ref,
            "labels": [{"name": l} for l in (labels or [])],
            "author": {"login": author},
        }

    def test_matches_branch_prefix(self, tmp_path):
        cfg = _make_cfg(tmp_path, branch_prefix="usr/at")
        assert s._is_hunter_pr(self._pr(head_ref="usr/at/fix-123"), cfg) is True

    def test_matches_sdd_proposal_label(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        assert s._is_hunter_pr(self._pr(labels=["sdd-proposal"]), cfg) is True

    def test_matches_sdd_implementation_label(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        assert s._is_hunter_pr(self._pr(labels=["sdd-implementation"]), cfg) is True

    def test_matches_author_equals_github_user(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        assert s._is_hunter_pr(self._pr(author="testuser"), cfg) is True

    def test_rejects_unrelated_pr(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        assert s._is_hunter_pr(self._pr(head_ref="feature/cool-thing", author="stranger"), cfg) is False


# ---------------------------------------------------------------------------
# TestConfigSentinelFields
# ---------------------------------------------------------------------------

class TestConfigSentinelFields:
    def test_defaults(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        assert cfg.post_ci_review_enabled is False
        assert cfg.max_open_auto_issues == 5
        assert cfg.auto_assign_filed_issues is True

    def test_post_ci_skill_path_default(self, tmp_path):
        data = {
            "repos": ["owner/repo"],
            "worktree_base": str(tmp_path),
            "github_user": "u",
        }
        cfg = s.Config(data)
        assert "post-ci-review" in str(cfg.post_ci_skill_path)

    def test_custom_values(self, tmp_path):
        cfg = _make_cfg(
            tmp_path,
            post_ci_review_enabled=True,
            max_open_auto_issues=10,
            auto_assign_filed_issues=False,
        )
        assert cfg.post_ci_review_enabled is True
        assert cfg.max_open_auto_issues == 10
        assert cfg.auto_assign_filed_issues is False

    def test_to_dict_includes_sentinel_fields(self, tmp_path):
        cfg = _make_cfg(tmp_path, post_ci_review_enabled=True, max_open_auto_issues=3)
        d = cfg.to_dict()
        assert "post_ci_review_enabled" in d
        assert d["post_ci_review_enabled"] is True
        assert d["max_open_auto_issues"] == 3
        assert "auto_assign_filed_issues" in d
        assert "post_ci_skill_path" in d
