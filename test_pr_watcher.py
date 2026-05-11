"""Tests for predd.py"""
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers to import the module (it's a script, not a package)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent))
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "predd", Path(__file__).parent / "predd.py"
)
pw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pw)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = """\
repos = ["owner/repo"]
worktree_base = "/tmp/pr-reviews"
github_user = "testuser"
"""


def _write_config(tmp_path: Path, content: str) -> Path:
    cfg_dir = tmp_path / ".config" / "predd"
    cfg_dir.mkdir(parents=True)
    cfg_file = cfg_dir / "config.toml"
    cfg_file.write_text(content)
    return cfg_file


class TestConfigLoading:
    def test_load_minimal_config(self, tmp_path, monkeypatch):
        cfg_file = _write_config(tmp_path, MINIMAL_CONFIG)
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        cfg = pw.load_config()
        assert cfg.repos == ["owner/repo"]
        assert cfg.github_user == "testuser"
        assert cfg.poll_interval == 90  # default

    def test_load_full_config(self, tmp_path, monkeypatch):
        content = MINIMAL_CONFIG + 'poll_interval = 120\nbackend = "claude"\nmodel = "claude-haiku-4-5"\n'
        cfg_file = _write_config(tmp_path, content)
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        cfg = pw.load_config()
        assert cfg.poll_interval == 120
        assert cfg.backend == "claude"
        assert cfg.model == "claude-haiku-4-5"

    def test_legacy_claude_model_key(self, tmp_path, monkeypatch):
        content = MINIMAL_CONFIG + 'claude_model = "claude-opus-4-7"\n'
        cfg_file = _write_config(tmp_path, content)
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        cfg = pw.load_config()
        assert cfg.model == "claude-opus-4-7"

    def test_default_backend_is_devin(self, tmp_path, monkeypatch):
        cfg_file = _write_config(tmp_path, MINIMAL_CONFIG)
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        cfg = pw.load_config()
        assert cfg.backend == "devin"
        assert cfg.model == "haiku-4-5"

    def test_missing_config_writes_template_and_exits(self, tmp_path, monkeypatch):
        missing = tmp_path / "nonexistent" / "config.toml"
        monkeypatch.setattr(pw, "CONFIG_FILE", missing)
        monkeypatch.setattr(pw, "CONFIG_DIR", missing.parent)
        with pytest.raises(SystemExit):
            pw.load_config()
        assert missing.exists()

    def test_config_to_dict(self, tmp_path, monkeypatch):
        cfg_file = _write_config(tmp_path, MINIMAL_CONFIG)
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        cfg = pw.load_config()
        d = cfg.to_dict()
        assert d["repos"] == ["owner/repo"]
        assert "github_user" in d

# ---------------------------------------------------------------------------
# State read/write atomicity
# ---------------------------------------------------------------------------

class TestStateAtomicWrite:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(pw, "STATE_FILE", state_file)
        monkeypatch.setattr(pw, "CONFIG_DIR", tmp_path)
        state = {"owner/repo#1": {"status": "awaiting_approval", "head_sha": "abc"}}
        pw.save_state(state)
        assert state_file.exists()
        loaded = pw.load_state()
        assert loaded == state

    def test_save_uses_tmp_then_rename(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        tmp_file = state_file.with_suffix(".json.tmp")
        monkeypatch.setattr(pw, "STATE_FILE", state_file)
        monkeypatch.setattr(pw, "CONFIG_DIR", tmp_path)
        pw.save_state({"k": "v"})
        # After save, .tmp should be gone and state.json should exist
        assert not tmp_file.exists()
        assert state_file.exists()

    def test_load_state_empty_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "no-state.json")
        assert pw.load_state() == {}

    def test_load_state_recovers_from_corrupt_file(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text("this is not json{{{")
        monkeypatch.setattr(pw, "STATE_FILE", state_file)
        result = pw.load_state()
        assert result == {}

    def test_update_pr_state_creates_entry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(pw, "CONFIG_DIR", tmp_path)
        state = {}
        pw.update_pr_state(state, "owner/repo#5", status="reviewing", head_sha="xyz")
        assert state["owner/repo#5"]["status"] == "reviewing"
        assert state["owner/repo#5"]["head_sha"] == "xyz"
        # Persisted
        loaded = pw.load_state()
        assert loaded["owner/repo#5"]["status"] == "reviewing"

# ---------------------------------------------------------------------------
# PR filter logic
# ---------------------------------------------------------------------------

class TestPRFilterLogic:
    """Tests for the skip conditions inside the daemon loop."""

    def _make_pr(self, author="other", is_draft=False, number=1, sha="abc123", review_requests=None):
        return {
            "number": number,
            "title": "Test PR",
            "author": {"login": author},
            "headRefOid": sha,
            "headRefName": "feature-branch",
            "isDraft": is_draft,
            "reviewRequests": review_requests or [],
        }

    def _should_process(self, pr: dict, state: dict, cfg_user: str,
                        trigger: str = "ready", repo: str = "owner/repo") -> bool:
        """Replicate the daemon loop filter logic."""
        if pr["author"]["login"] == cfg_user:
            return False
        if pr["isDraft"]:
            return False
        if trigger == "requested":
            requested = [r.get("login") for r in pr.get("reviewRequests", [])]
            if cfg_user not in requested:
                return False
        key = f"{repo}#{pr['number']}"
        entry = state.get(key, {})
        entry_sha = entry.get("head_sha", "")
        entry_status = entry.get("status", "")
        if entry_sha == pr["headRefOid"] and entry_status in (
            "submitted", "rejected", "awaiting_approval", "reviewing"
        ):
            return False
        return True

    def test_skip_own_pr(self):
        pr = self._make_pr(author="myuser")
        assert not self._should_process(pr, {}, "myuser")

    def test_skip_draft_pr(self):
        pr = self._make_pr(is_draft=True)
        assert not self._should_process(pr, {}, "myuser")

    def test_skip_already_submitted(self):
        pr = self._make_pr(sha="abc")
        state = {"owner/repo#1": {"head_sha": "abc", "status": "submitted"}}
        assert not self._should_process(pr, state, "myuser")

    def test_skip_already_rejected(self):
        pr = self._make_pr(sha="abc")
        state = {"owner/repo#1": {"head_sha": "abc", "status": "rejected"}}
        assert not self._should_process(pr, state, "myuser")

    def test_skip_awaiting_approval(self):
        pr = self._make_pr(sha="abc")
        state = {"owner/repo#1": {"head_sha": "abc", "status": "awaiting_approval"}}
        assert not self._should_process(pr, state, "myuser")

    def test_skip_currently_reviewing(self):
        pr = self._make_pr(sha="abc")
        state = {"owner/repo#1": {"head_sha": "abc", "status": "reviewing"}}
        assert not self._should_process(pr, state, "myuser")

    def test_process_new_pr(self):
        pr = self._make_pr(sha="abc")
        assert self._should_process(pr, {}, "myuser")

    def test_process_updated_sha(self):
        """If sha changes, treat as new review event even if already submitted."""
        pr = self._make_pr(sha="newsha")
        state = {"owner/repo#1": {"head_sha": "oldsha", "status": "submitted"}}
        assert self._should_process(pr, state, "myuser")

    def test_process_failed_pr(self):
        """A failed PR should be retried."""
        pr = self._make_pr(sha="abc")
        state = {"owner/repo#1": {"head_sha": "abc", "status": "failed"}}
        assert self._should_process(pr, state, "myuser")

    # trigger=requested
    def test_requested_mode_skips_when_not_requested(self):
        pr = self._make_pr(review_requests=[])
        assert not self._should_process(pr, {}, "myuser", trigger="requested")

    def test_requested_mode_skips_when_other_user_requested(self):
        pr = self._make_pr(review_requests=[{"login": "otheruser"}])
        assert not self._should_process(pr, {}, "myuser", trigger="requested")

    def test_requested_mode_processes_when_user_requested(self):
        pr = self._make_pr(review_requests=[{"login": "myuser"}])
        assert self._should_process(pr, {}, "myuser", trigger="requested")

    def test_ready_mode_ignores_review_requests(self):
        pr = self._make_pr(review_requests=[])
        assert self._should_process(pr, {}, "myuser", trigger="ready")

# ---------------------------------------------------------------------------
# gh subprocess wrapper
# ---------------------------------------------------------------------------

class TestGhSubprocessWrapper:
    def test_gh_list_open_prs_calls_gh_correctly(self):
        fake_prs = [
            {"number": 1, "title": "Fix bug", "author": {"login": "someone"},
             "headRefOid": "abc123", "headRefName": "fix-bug", "isDraft": False}
        ]
        fake_result = MagicMock()
        fake_result.stdout = json.dumps(fake_prs)

        with patch("subprocess.run", return_value=fake_result) as mock_run:
            result = pw.gh_list_open_prs("owner/repo")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "gh"
        assert "pr" in args
        assert "list" in args
        assert "--repo" in args
        assert "owner/repo" in args
        assert result == fake_prs

    def test_gh_pr_view_calls_gh_correctly(self):
        fake_result = MagicMock()
        fake_result.stdout = "PR title: Fix bug\n"
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            result = pw.gh_pr_view("owner/repo", 42)
        args = mock_run.call_args[0][0]
        assert "view" in args
        assert "42" in args
        assert "--repo" in args
        assert "owner/repo" in args

    def test_gh_pr_diff_calls_gh_correctly(self):
        fake_result = MagicMock()
        fake_result.stdout = "diff --git a/foo.py b/foo.py\n"
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            pw.gh_pr_diff("owner/repo", 42)
        args = mock_run.call_args[0][0]
        assert "diff" in args
        assert "42" in args

    def test_gh_pr_review_approve(self, tmp_path):
        body_file = tmp_path / "body.md"
        body_file.write_text("LGTM")
        fake_result = MagicMock()
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            pw.gh_pr_review("owner/repo", 42, "approve", body_file)
        args = mock_run.call_args[0][0]
        assert "--approve" in args
        assert "--body-file" in args

    def test_gh_pr_review_request_changes(self, tmp_path):
        body_file = tmp_path / "body.md"
        body_file.write_text("Needs work")
        fake_result = MagicMock()
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            pw.gh_pr_review("owner/repo", 42, "request-changes", body_file)
        args = mock_run.call_args[0][0]
        assert "--request-changes" in args

    def _fake_view(self, state="OPEN", reviews=None):
        fake = MagicMock()
        fake.stdout = json.dumps({"state": state, "reviews": reviews or []})
        return fake

    def test_already_reviewed_returns_false_for_open_unreviewed(self):
        with patch("subprocess.run", return_value=self._fake_view()):
            assert pw.gh_pr_already_reviewed("owner/repo", 1, "myuser") is False

    def test_already_reviewed_returns_true_when_merged(self):
        with patch("subprocess.run", return_value=self._fake_view(state="MERGED")):
            assert pw.gh_pr_already_reviewed("owner/repo", 1, "myuser") is True

    def test_already_reviewed_returns_true_when_closed(self):
        with patch("subprocess.run", return_value=self._fake_view(state="CLOSED")):
            assert pw.gh_pr_already_reviewed("owner/repo", 1, "myuser") is True

    def test_already_reviewed_returns_true_when_user_reviewed(self):
        reviews = [{"author": {"login": "myuser"}, "state": "APPROVED"}]
        with patch("subprocess.run", return_value=self._fake_view(reviews=reviews)):
            assert pw.gh_pr_already_reviewed("owner/repo", 1, "myuser") is True

    def test_already_reviewed_returns_false_when_other_user_reviewed(self):
        reviews = [{"author": {"login": "otheruser"}, "state": "APPROVED"}]
        with patch("subprocess.run", return_value=self._fake_view(reviews=reviews)):
            assert pw.gh_pr_already_reviewed("owner/repo", 1, "myuser") is False

# ---------------------------------------------------------------------------
# Notify functions are mockable
# ---------------------------------------------------------------------------

class TestNotifyFunctions:
    """Ensure notify_sound and notify_toast are standalone functions (mockable)."""

    def test_notify_sound_calls_pwsh(self):
        with patch.object(pw, "_PWSH", "pwsh.exe"), patch("subprocess.run") as mock_run:
            pw.notify_sound("C:\\sounds\\ping.wav")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "pwsh.exe"
        assert "SoundPlayer" in " ".join(args)

    def test_notify_toast_calls_pwsh(self):
        with patch.object(pw, "_PWSH", "pwsh.exe"), patch("subprocess.run") as mock_run:
            pw.notify_toast("Title", "Body text")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "pwsh.exe"
        assert "BurntToast" in " ".join(args)

    def test_notify_sound_empty_path_skips(self):
        with patch.object(pw, "_PWSH", "pwsh.exe"), patch("subprocess.run") as mock_run:
            pw.notify_sound("")
        mock_run.assert_not_called()

    def test_notify_skips_when_pwsh_not_found(self):
        with patch.object(pw, "_PWSH", None), patch("subprocess.run") as mock_run:
            pw.notify_sound("C:\\sounds\\ping.wav")
            pw.notify_toast("Title", "Body")
        mock_run.assert_not_called()

    def test_notify_failures_do_not_raise(self):
        with patch.object(pw, "_PWSH", "pwsh.exe"), \
             patch("subprocess.run", side_effect=Exception("boom")):
            pw.notify_sound("C:\\sounds\\ping.wav")
            pw.notify_toast("Title", "Body")

# ---------------------------------------------------------------------------
# PR arg parsing
# ---------------------------------------------------------------------------

class TestParsePrArg:
    def test_full_ref(self):
        repo, num = pw.parse_pr_arg("owner/repo#42")
        assert repo == "owner/repo"
        assert num == 42

    def test_number_only(self):
        repo, num = pw.parse_pr_arg("42")
        assert repo is None
        assert num == 42

    def test_invalid_raises(self):
        import click
        with pytest.raises(click.BadParameter):
            pw.parse_pr_arg("not-a-pr")

# ---------------------------------------------------------------------------
# resolve_pr_key
# ---------------------------------------------------------------------------

class TestResolvePrKey:
    def _state(self):
        return {
            "owner/repo#10": {"status": "awaiting_approval", "head_sha": "abc"},
            "owner/repo#20": {"status": "submitted", "head_sha": "def"},
        }

    def test_resolve_by_number(self):
        state = self._state()
        repo, num, entry = pw.resolve_pr_key(state, "10")
        assert repo == "owner/repo"
        assert num == 10

    def test_resolve_by_full_ref(self):
        state = self._state()
        repo, num, entry = pw.resolve_pr_key(state, "owner/repo#10")
        assert num == 10

    def test_not_found_raises(self):
        import click
        state = self._state()
        with pytest.raises(click.ClickException):
            pw.resolve_pr_key(state, "999")

    def test_ambiguous_raises(self):
        import click
        state = {
            "org/repo-a#5": {"status": "awaiting_approval"},
            "org/repo-b#5": {"status": "awaiting_approval"},
        }
        with pytest.raises(click.ClickException):
            pw.resolve_pr_key(state, "5")

# ---------------------------------------------------------------------------
# Backend drivers
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path, backend: str, model: str = "haiku-4-5") -> pw.Config:
    return pw.Config({
        "repos": ["owner/repo"],
        "worktree_base": str(tmp_path / "worktrees"),
        "github_user": "testuser",
        "backend": backend,
        "model": model,
        "skill_path": str(tmp_path / "SKILL.md"),
    })


def _fake_run_proc(output=""):
    """Patch target for _run_proc — captures cmd and worktree."""
    captured = {}
    def _inner(cmd, worktree, env=None):
        captured["cmd"] = cmd
        captured["worktree"] = worktree
        captured["env"] = env
        return output
    return _inner, captured


class TestClaudeDriver:
    def test_invokes_claude_p(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("Review PR $ARGUMENTS please.")
        cfg = _make_cfg(tmp_path, "claude", "claude-opus-4-7")
        cfg.skill_path = skill
        worktree = tmp_path / "wt"; worktree.mkdir()

        fn, cap = _fake_run_proc("LGTM")
        with patch.object(pw, "_run_proc", side_effect=fn):
            result = pw._run_claude(cfg, "Review PR 42 please.", worktree)

        assert cap["cmd"][0] == "claude"
        assert "-p" in cap["cmd"]
        assert "--model" in cap["cmd"]
        assert "claude-opus-4-7" in cap["cmd"]
        assert result == "LGTM"

    def test_runs_in_worktree(self, tmp_path):
        cfg = _make_cfg(tmp_path, "claude")
        worktree = tmp_path / "wt"; worktree.mkdir()
        fn, cap = _fake_run_proc()
        with patch.object(pw, "_run_proc", side_effect=fn):
            pw._run_claude(cfg, "prompt", worktree)
        assert cap["worktree"] == worktree


class TestDevinDriver:
    def test_invokes_setsid_devin(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("Review PR $ARGUMENTS please.")
        cfg = _make_cfg(tmp_path, "devin", "haiku-4-5")
        cfg.skill_path = skill
        worktree = tmp_path / "wt"; worktree.mkdir()

        fn, cap = _fake_run_proc("Review posted.")
        with patch.object(pw, "_run_proc", side_effect=fn):
            result = pw._run_devin(cfg, "Review PR 42 please.", worktree)

        assert cap["cmd"][0] == "setsid"
        assert cap["cmd"][1] == "devin"
        assert "-p" in cap["cmd"]
        assert "--permission-mode" in cap["cmd"]
        assert "auto" in cap["cmd"]
        assert "--model" in cap["cmd"]
        assert "haiku-4-5" in cap["cmd"]
        assert "--" in cap["cmd"]
        assert result == "Review posted."

    def test_strips_claude_env_vars(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("prompt")
        cfg = _make_cfg(tmp_path, "devin")
        cfg.skill_path = skill
        worktree = tmp_path / "wt"; worktree.mkdir()

        fn, cap = _fake_run_proc()
        with patch.dict(os.environ, {
            "CLAUDECODE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "HOME": "/home/testuser",
        }):
            with patch.object(pw, "_run_proc", side_effect=fn):
                pw._run_devin(cfg, "prompt", worktree)

        assert "CLAUDECODE" not in cap["env"]
        assert "CLAUDE_CODE_ENTRYPOINT" not in cap["env"]
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in cap["env"]
        assert "HOME" in cap["env"]

    def test_places_skill_in_devin_skills_dir(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("the skill content")
        cfg = _make_cfg(tmp_path, "devin")
        cfg.skill_path = skill
        worktree = tmp_path / "wt"; worktree.mkdir()

        fn, _ = _fake_run_proc()
        with patch.object(pw, "_run_proc", side_effect=fn):
            pw._run_devin(cfg, "prompt", worktree)

        placed = worktree / ".devin" / "skills" / "pr-review.md"
        assert placed.exists()
        assert placed.read_text() == "the skill content"

    def test_runs_in_worktree(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("prompt")
        cfg = _make_cfg(tmp_path, "devin")
        cfg.skill_path = skill
        worktree = tmp_path / "wt"; worktree.mkdir()
        fn, cap = _fake_run_proc()
        with patch.object(pw, "_run_proc", side_effect=fn):
            pw._run_devin(cfg, "prompt", worktree)
        assert cap["worktree"] == worktree


class TestRunReviewDispatch:
    def test_dispatches_to_devin(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("Review PR $ARGUMENTS")
        cfg = _make_cfg(tmp_path, "devin")
        cfg.skill_path = skill
        worktree = tmp_path / "wt"; worktree.mkdir()
        with patch.object(pw, "_run_devin", return_value="ok") as mock_devin:
            pw.run_review(cfg, "owner/repo", 42, worktree)
        mock_devin.assert_called_once()

    def test_dispatches_to_claude(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("Review PR $ARGUMENTS")
        cfg = _make_cfg(tmp_path, "claude")
        cfg.skill_path = skill
        worktree = tmp_path / "wt"; worktree.mkdir()
        with patch.object(pw, "_run_claude", return_value="ok") as mock_claude:
            pw.run_review(cfg, "owner/repo", 42, worktree)
        mock_claude.assert_called_once()

    def test_unknown_backend_raises(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("prompt")
        cfg = _make_cfg(tmp_path, "devin")
        cfg.skill_path = skill
        cfg.backend = "gpt-banana"
        worktree = tmp_path / "wt"; worktree.mkdir()
        with pytest.raises(ValueError, match="Unknown backend"):
            pw.run_review(cfg, "owner/repo", 42, worktree)

    def test_arguments_substituted_in_prompt(self, tmp_path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("Review PR $ARGUMENTS now.")
        cfg = _make_cfg(tmp_path, "devin")
        cfg.skill_path = skill
        worktree = tmp_path / "wt"; worktree.mkdir()
        captured = {}
        def fake_devin(cfg, prompt, wt):
            captured["prompt"] = prompt
            return ""
        with patch.object(pw, "_run_devin", side_effect=fake_devin):
            pw.run_review(cfg, "owner/repo", 99, worktree)
        assert "99" in captured["prompt"]
        assert "$ARGUMENTS" not in captured["prompt"]
