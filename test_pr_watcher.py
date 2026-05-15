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
        assert cfg.model == "swe-1.6"

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
        # New schema: repo list, not flat repos key
        assert "repo" in d
        assert d["repo"][0]["name"] == "owner/repo"
        assert "github_user" in d

    def test_config_load_default_sprint_filter(self, tmp_path, monkeypatch):
        """Config without jira_sprint_filter defaults to 'active'."""
        cfg_file = _write_config(tmp_path, MINIMAL_CONFIG)
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        cfg = pw.load_config()
        assert cfg.jira_sprint_filter == "active"
        assert cfg.jira_active_sprint_name == ""

    def test_config_load_invalid_sprint_filter(self, tmp_path, monkeypatch, caplog):
        """Unrecognized jira_sprint_filter logs a warning and defaults to 'active'."""
        import logging
        content = MINIMAL_CONFIG + 'jira_sprint_filter = "bogus-value"\n'
        cfg_file = _write_config(tmp_path, content)
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        with caplog.at_level(logging.WARNING, logger="predd"):
            cfg = pw.load_config()
        assert cfg.jira_sprint_filter == "active"
        assert any("jira_sprint_filter" in r.message for r in caplog.records)

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
        fake_result.returncode = 0
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
        fake_result.returncode = 0
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
        fake_result.returncode = 0
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
        fake_result.returncode = 0
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            pw.gh_pr_review("owner/repo", 42, "approve", body_file)
        args = mock_run.call_args[0][0]
        assert "--approve" in args
        assert "--body-file" in args

    def test_gh_pr_review_request_changes(self, tmp_path):
        body_file = tmp_path / "body.md"
        body_file.write_text("Needs work")
        fake_result = MagicMock()
        fake_result.returncode = 0
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            pw.gh_pr_review("owner/repo", 42, "request-changes", body_file)
        args = mock_run.call_args[0][0]
        assert "--request-changes" in args

    def _fake_view(self, state="OPEN", reviews=None):
        fake = MagicMock()
        fake.returncode = 0
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
    def _inner(cmd, worktree, env=None, stdin_text=None):
        captured["cmd"] = cmd
        captured["worktree"] = worktree
        captured["env"] = env
        captured["stdin_text"] = stdin_text
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
        assert cap["stdin_text"] == "Review PR 42 please."
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

        # stem of "SKILL.md" is "SKILL", lowercased → "skill"
        placed = worktree / ".devin" / "skills" / "skill.md"
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


# ---------------------------------------------------------------------------
# Bedrock backend: construction + prompt caching
# ---------------------------------------------------------------------------

def _make_bedrock_mocks():
    """Create a fake anthropic module with a mock AnthropicBedrock class."""
    mock_resp = MagicMock()
    mock_resp.stop_reason = "end_turn"
    mock_resp.content = [MagicMock(type="text", text="done")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mock_bedrock_cls = MagicMock(return_value=mock_client)
    fake_anthropic = MagicMock()
    fake_anthropic.AnthropicBedrock = mock_bedrock_cls
    return fake_anthropic, mock_bedrock_cls, mock_client


class TestBedrockDriver:
    def test_client_constructed_with_aws_region_only(self, tmp_path):
        """AnthropicBedrock must be called with aws_region=, not region_name= or aws_profile=."""
        skill = tmp_path / "SKILL.md"
        skill.write_text("Review PR $ARGUMENTS")
        cfg = _make_cfg(tmp_path, "bedrock")
        cfg.skill_path = skill
        cfg.aws_region = "us-west-2"
        cfg.aws_profile = "default"
        cfg.bedrock_model = "eu.anthropic.claude-3-7-sonnet-20250219-v1:0"
        worktree = tmp_path / "wt"; worktree.mkdir()

        fake_anthropic, mock_bedrock_cls, _ = _make_bedrock_mocks()
        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            pw._run_bedrock_skill(cfg, "test prompt", skill, worktree)

        mock_bedrock_cls.assert_called_once_with(aws_region="us-west-2")

    def test_system_has_cache_control(self, tmp_path):
        """System arg must be a list with cache_control on the SKILL.md block."""
        skill = tmp_path / "SKILL.md"
        skill.write_text("skill content here")
        cfg = _make_cfg(tmp_path, "bedrock")
        cfg.skill_path = skill
        cfg.aws_region = "us-east-1"
        cfg.aws_profile = "default"
        cfg.bedrock_model = "eu.anthropic.claude-3-7-sonnet-20250219-v1:0"
        worktree = tmp_path / "wt"; worktree.mkdir()

        fake_anthropic, _, mock_client = _make_bedrock_mocks()
        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            pw._run_bedrock_skill(cfg, "test prompt", skill, worktree)

        create_kwargs = mock_client.messages.create.call_args
        system_arg = create_kwargs.kwargs.get("system") or create_kwargs[1].get("system")
        assert isinstance(system_arg, list), "system must be a list of content blocks"
        assert len(system_arg) == 2
        assert system_arg[1]["cache_control"] == {"type": "ephemeral"}
        assert "skill content here" in system_arg[1]["text"]

    def test_aws_profile_set_in_env_when_not_default(self, tmp_path, monkeypatch):
        """Non-default aws_profile should be set as AWS_PROFILE env var."""
        skill = tmp_path / "SKILL.md"
        skill.write_text("skill content")
        cfg = _make_cfg(tmp_path, "bedrock")
        cfg.skill_path = skill
        cfg.aws_region = "us-east-1"
        cfg.aws_profile = "my-profile"
        cfg.bedrock_model = "eu.anthropic.claude-3-7-sonnet-20250219-v1:0"
        worktree = tmp_path / "wt"; worktree.mkdir()

        fake_anthropic, _, _ = _make_bedrock_mocks()
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            pw._run_bedrock_skill(cfg, "test prompt", skill, worktree)

        assert os.environ.get("AWS_PROFILE") == "my-profile"


# ---------------------------------------------------------------------------
# gh_run permanent error detection (SPEC 6)
# ---------------------------------------------------------------------------

class TestGhRunPermanentErrors:
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
                pw.gh_run(["issue", "view", "1"])
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    def test_transient_error_retries(self):
        """Rate limit errors should be retried."""
        fail = self._make_result(1, "error: rate limit exceeded")
        ok = MagicMock(); ok.returncode = 0; ok.stdout = "[]"
        with patch("subprocess.run", side_effect=[fail, ok]) as mock_run, \
             patch("time.sleep"):
            result = pw.gh_run(["pr", "list"])
        assert mock_run.call_count == 2

    def test_unknown_error_fails_immediately(self):
        """Unknown errors should not be retried."""
        result = self._make_result(1, "error: something unexpected happened")
        with patch("subprocess.run", return_value=result) as mock_run, \
             patch("time.sleep") as mock_sleep:
            with pytest.raises(subprocess.CalledProcessError):
                pw.gh_run(["pr", "list"])
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    def test_check_false_returns_on_error(self):
        """check=False should return even on error."""
        result = self._make_result(1, "error: not found")
        with patch("subprocess.run", return_value=result):
            r = pw.gh_run(["issue", "view", "1"], check=False)
        assert r.returncode == 1

    def test_403_fails_immediately(self):
        result = self._make_result(1, "error: 403 forbidden")
        with patch("subprocess.run", return_value=result) as mock_run, \
             patch("time.sleep") as mock_sleep:
            with pytest.raises(subprocess.CalledProcessError):
                pw.gh_run(["pr", "list"])
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Per-repo config (RepoConfig / _load_repo_configs / Config accessors)
# ---------------------------------------------------------------------------

_REPO_BASE_FIELDS = 'worktree_base = "/tmp/pr-reviews"\ngithub_user = "testuser"\n\n'


class TestRepoConfig:
    def _cfg(self, toml_text: str, tmp_path, monkeypatch) -> pw.Config:
        cfg_dir = tmp_path / ".config" / "predd"
        cfg_dir.mkdir(parents=True)
        cfg_file = cfg_dir / "config.toml"
        # Base fields must come before [[repo]] blocks (TOML array-of-tables)
        cfg_file.write_text(_REPO_BASE_FIELDS + toml_text)
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        return pw.load_config()

    # ---- New schema --------------------------------------------------------

    def test_new_schema_parses(self, tmp_path, monkeypatch):
        toml = (
            '[[repo]]\nname = "owner/a"\npredd = true\nhunter = true\nobsidian = true\n\n'
            '[[repo]]\nname = "owner/b"\npredd = false\nhunter = true\nobsidian = false\n'
        )
        cfg = self._cfg(toml, tmp_path, monkeypatch)
        assert len(cfg.repo_configs) == 2
        assert cfg.repo_configs[0].name == "owner/a"
        assert cfg.repo_configs[1].name == "owner/b"
        assert cfg.repo_configs[1].predd is False
        assert cfg.repo_configs[1].hunter is True
        assert cfg.repo_configs[1].obsidian is False

    # ---- Old flat schema ---------------------------------------------------

    def test_old_schema_repos(self, tmp_path, monkeypatch):
        toml = 'repos = ["owner/a", "owner/b"]\n'
        cfg = self._cfg(toml, tmp_path, monkeypatch)
        assert cfg.repos_for("predd") == ["owner/a", "owner/b"]
        assert cfg.repos_for("hunter") == ["owner/a", "owner/b"]
        assert cfg.repos_for("obsidian") == ["owner/a", "owner/b"]

    def test_old_schema_predd_only_repos(self, tmp_path, monkeypatch):
        toml = 'repos = ["owner/a"]\npredd_only_repos = ["owner/p"]\nhunter_only_repos = ["owner/h"]\n'
        cfg = self._cfg(toml, tmp_path, monkeypatch)
        assert "owner/p" in cfg.repos_for("predd")
        assert "owner/p" not in cfg.repos_for("hunter")
        assert "owner/h" in cfg.repos_for("hunter")
        assert "owner/h" not in cfg.repos_for("predd")
        assert "owner/p" not in cfg.repos_for("obsidian")
        assert "owner/h" not in cfg.repos_for("obsidian")

    def test_old_schema_logs_deprecation(self, tmp_path, monkeypatch, caplog):
        toml = 'repos = ["owner/a"]\npredd_only_repos = ["owner/p"]\n'
        import logging
        with caplog.at_level(logging.INFO, logger="predd"):
            self._cfg(toml, tmp_path, monkeypatch)
        assert any("legacy flat schema" in r.message for r in caplog.records)

    # ---- Both schemas present ----------------------------------------------

    def test_both_schemas_uses_new(self, tmp_path, monkeypatch, caplog):
        toml = (
            'repos = ["owner/old"]\n'
            '[[repo]]\nname = "owner/new"\npredd = true\nhunter = false\nobsidian = true\n'
        )
        import logging
        with caplog.at_level(logging.WARNING, logger="predd"):
            cfg = self._cfg(toml, tmp_path, monkeypatch)
        assert cfg.repos_for("predd") == ["owner/new"]
        assert cfg.repos_for("hunter") == []
        assert any("both" in r.message for r in caplog.records)

    # ---- Accessor methods --------------------------------------------------

    def test_repos_for_predd(self, tmp_path, monkeypatch):
        toml = (
            '[[repo]]\nname = "owner/a"\npredd = true\nhunter = true\nobsidian = true\n\n'
            '[[repo]]\nname = "owner/b"\npredd = false\nhunter = true\nobsidian = true\n'
        )
        cfg = self._cfg(toml, tmp_path, monkeypatch)
        assert cfg.repos_for("predd") == ["owner/a"]

    def test_repos_for_hunter(self, tmp_path, monkeypatch):
        toml = (
            '[[repo]]\nname = "owner/a"\npredd = true\nhunter = true\nobsidian = true\n\n'
            '[[repo]]\nname = "owner/b"\npredd = true\nhunter = false\nobsidian = true\n'
        )
        cfg = self._cfg(toml, tmp_path, monkeypatch)
        assert cfg.repos_for("hunter") == ["owner/a"]

    def test_repos_for_obsidian(self, tmp_path, monkeypatch):
        toml = (
            '[[repo]]\nname = "owner/a"\npredd = true\nhunter = true\nobsidian = false\n\n'
            '[[repo]]\nname = "owner/b"\npredd = true\nhunter = true\nobsidian = true\n'
        )
        cfg = self._cfg(toml, tmp_path, monkeypatch)
        assert cfg.repos_for("obsidian") == ["owner/b"]

    def test_repo_config_lookup(self, tmp_path, monkeypatch):
        toml = '[[repo]]\nname = "owner/a"\npredd = true\nhunter = false\nobsidian = true\n'
        cfg = self._cfg(toml, tmp_path, monkeypatch)
        rc = cfg.repo_config("owner/a")
        assert rc is not None
        assert rc.hunter is False
        assert cfg.repo_config("owner/missing") is None


    # ---- Backward-compat properties ----------------------------------------

    def test_repos_property_deduped(self, tmp_path, monkeypatch):
        toml = (
            '[[repo]]\nname = "owner/a"\n\n'
            '[[repo]]\nname = "owner/b"\npredd = false\nhunter = true\nobsidian = false\n'
        )
        cfg = self._cfg(toml, tmp_path, monkeypatch)
        assert cfg.repos == ["owner/a", "owner/b"]

    def test_predd_only_repos_property(self, tmp_path, monkeypatch):
        toml = (
            '[[repo]]\nname = "owner/a"\npredd = true\nhunter = false\nobsidian = false\n\n'
            '[[repo]]\nname = "owner/b"\npredd = true\nhunter = true\nobsidian = true\n'
        )
        cfg = self._cfg(toml, tmp_path, monkeypatch)
        assert cfg.predd_only_repos == ["owner/a"]

    def test_hunter_only_repos_property(self, tmp_path, monkeypatch):
        toml = (
            '[[repo]]\nname = "owner/a"\npredd = false\nhunter = true\nobsidian = false\n\n'
            '[[repo]]\nname = "owner/b"\npredd = true\nhunter = true\nobsidian = true\n'
        )
        cfg = self._cfg(toml, tmp_path, monkeypatch)
        assert cfg.hunter_only_repos == ["owner/a"]

    # ---- Empty config edge case --------------------------------------------

    def test_empty_repos_new_schema(self, tmp_path, monkeypatch):
        toml = ""
        cfg = self._cfg(toml, tmp_path, monkeypatch)
        assert cfg.repo_configs == []
        assert cfg.repos_for("predd") == []
        assert cfg.repos_for("hunter") == []
        assert cfg.repos_for("obsidian") == []


# ---------------------------------------------------------------------------
# Config wizard and config commands
# ---------------------------------------------------------------------------

class TestConfigToDict:
    """Config.to_dict() must emit the new [[repo]] schema."""

    def _cfg_from_toml(self, toml_str: str, tmp_path, monkeypatch):
        cfg_file = _write_config(tmp_path, toml_str)
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        return pw.load_config()

    def test_to_dict_has_repo_list(self, tmp_path, monkeypatch):
        toml = (
            'worktree_base = "/tmp/wt"\ngithub_user = "alice"\n'
            '[[repo]]\nname = "owner/repo"\npredd = true\nhunter = true\nobsidian = false\n'
        )
        cfg = self._cfg_from_toml(toml, tmp_path, monkeypatch)
        d = cfg.to_dict()
        assert "repo" in d
        assert isinstance(d["repo"], list)
        assert len(d["repo"]) == 1
        assert d["repo"][0]["name"] == "owner/repo"
        assert d["repo"][0]["predd"] is True
        assert d["repo"][0]["hunter"] is True
        assert d["repo"][0]["obsidian"] is False

    def test_to_dict_no_flat_repo_keys(self, tmp_path, monkeypatch):
        """to_dict() should not include old-schema keys repos/predd_only_repos."""
        cfg = self._cfg_from_toml(
            'worktree_base = "/tmp/wt"\ngithub_user = "alice"\n',
            tmp_path, monkeypatch,
        )
        d = cfg.to_dict()
        assert "repos" not in d
        assert "predd_only_repos" not in d
        assert "hunter_only_repos" not in d

    def test_to_dict_scalar_fields(self, tmp_path, monkeypatch):
        cfg = self._cfg_from_toml(
            'worktree_base = "/tmp/wt"\ngithub_user = "alice"\nbackend = "claude"\n',
            tmp_path, monkeypatch,
        )
        d = cfg.to_dict()
        assert d["github_user"] == "alice"
        assert d["backend"] == "claude"
        assert "worktree_base" in d

    def test_to_dict_jira_csv_dir_not_in_repo_entry(self, tmp_path, monkeypatch):
        toml = (
            'worktree_base = "/tmp/wt"\ngithub_user = "alice"\n'
            '[[repo]]\nname = "owner/repo"\npredd = true\nhunter = true\nobsidian = false\n'
        )
        cfg = self._cfg_from_toml(toml, tmp_path, monkeypatch)
        d = cfg.to_dict()
        assert "jira_csv_dir" not in d["repo"][0]


class TestSerializeConfigToml:
    """_serialize_config_toml produces valid TOML that round-trips."""

    def test_scalar_fields_rendered(self):
        d = {
            "github_user": "bob",
            "worktree_base": "/tmp/wt",
            "backend": "devin",
            "max_review_fix_loops": 2,
            "auto_review_draft": False,
            "repo": [],
        }
        out = pw._serialize_config_toml(d)
        assert 'github_user = "bob"' in out
        assert "max_review_fix_loops = 2" in out
        assert "auto_review_draft = false" in out

    def test_repo_blocks_rendered(self):
        d = {
            "github_user": "bob",
            "repo": [
                {"name": "owner/repo", "predd": True, "hunter": False, "obsidian": False}
            ],
        }
        out = pw._serialize_config_toml(d)
        assert "[[repo]]" in out
        assert 'name = "owner/repo"' in out
        assert "predd = true" in out
        assert "hunter = false" in out

    def test_none_values_omitted(self):
        d = {"github_user": "bob", "jira_csv_dir": None, "repo": []}
        out = pw._serialize_config_toml(d)
        assert "jira_csv_dir" not in out

    def test_roundtrip_via_tomllib(self, tmp_path):
        import tomllib as tl
        d = {
            "github_user": "alice",
            "worktree_base": "/tmp/wt",
            "backend": "devin",
            "max_review_fix_loops": 1,
            "auto_review_draft": False,
            "repo": [
                {"name": "owner/repo", "predd": True, "hunter": True, "obsidian": False}
            ],
        }
        out = pw._serialize_config_toml(d)
        path = tmp_path / "test.toml"
        path.write_text(out)
        loaded = tl.loads(out)
        assert loaded["github_user"] == "alice"
        assert loaded["repo"][0]["name"] == "owner/repo"


class TestWriteConfigAtomic:
    def test_writes_via_tmp_then_renames(self, tmp_path, monkeypatch):
        dest = tmp_path / "config.toml"
        monkeypatch.setattr(pw, "CONFIG_FILE", dest)
        d = {"github_user": "carol", "repo": []}
        pw._write_config_atomic(d, dest)
        assert dest.exists()
        assert not (tmp_path / "config.toml.tmp").exists()
        content = dest.read_text()
        assert "carol" in content

    def test_original_unchanged_on_rename_failure(self, tmp_path, monkeypatch):
        dest = tmp_path / "config.toml"
        dest.write_text("original content\n")

        original_rename = Path.rename

        def fail_rename(self, target):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "rename", fail_rename)
        with pytest.raises(OSError):
            pw._write_config_atomic({"github_user": "new", "repo": []}, dest)
        assert dest.read_text() == "original content\n"


class TestConfigShow:
    def test_prints_fields(self, tmp_path, monkeypatch, capsys):
        cfg_file = _write_config(
            tmp_path,
            'worktree_base = "/tmp/wt"\ngithub_user = "alice"\nbackend = "devin"\n'
            '[[repo]]\nname = "owner/repo"\npredd = true\nhunter = true\nobsidian = false\n',
        )
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(pw.cli, ["config", "show"])
        assert result.exit_code == 0, result.output
        assert "github_user" in result.output
        assert "alice" in result.output
        assert "owner/repo" in result.output

    def test_exits_nonzero_when_no_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pw, "CONFIG_FILE", tmp_path / "nonexistent.toml")
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(pw.cli, ["config", "show"])
        assert result.exit_code != 0

    def test_config_group_default_shows(self, tmp_path, monkeypatch, capsys):
        """'predd config' with no subcommand should behave like 'predd config show'."""
        cfg_file = _write_config(
            tmp_path,
            'worktree_base = "/tmp/wt"\ngithub_user = "alice"\n'
            '[[repo]]\nname = "owner/repo"\npredd = true\nhunter = true\nobsidian = false\n',
        )
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(pw.cli, ["config"])
        assert result.exit_code == 0, result.output
        assert "github_user" in result.output


class TestConfigSet:
    def test_set_scalar_updates_field(self, tmp_path, monkeypatch):
        cfg_file = _write_config(
            tmp_path,
            'worktree_base = "/tmp/wt"\ngithub_user = "alice"\nbackend = "devin"\n'
            '[[repo]]\nname = "owner/repo"\npredd = true\nhunter = true\nobsidian = false\n',
        )
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(pw.cli, ["config", "set", "backend", "claude"])
        assert result.exit_code == 0, result.output

        # Reload and verify
        import tomllib
        with open(cfg_file, "rb") as f:
            data = tomllib.load(f)
        assert data["backend"] == "claude"

    def test_set_integer_field(self, tmp_path, monkeypatch):
        cfg_file = _write_config(
            tmp_path,
            'worktree_base = "/tmp/wt"\ngithub_user = "alice"\n'
            '[[repo]]\nname = "owner/repo"\npredd = true\nhunter = true\nobsidian = false\n',
        )
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(pw.cli, ["config", "set", "max_review_fix_loops", "3"])
        assert result.exit_code == 0, result.output
        import tomllib
        with open(cfg_file, "rb") as f:
            data = tomllib.load(f)
        assert data["max_review_fix_loops"] == 3

    def test_set_unknown_key_exits_nonzero(self, tmp_path, monkeypatch):
        cfg_file = _write_config(tmp_path, 'worktree_base = "/tmp/wt"\ngithub_user = "alice"\n')
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(pw.cli, ["config", "set", "not_a_real_key", "value"])
        assert result.exit_code != 0

    def test_set_leaves_other_fields_unchanged(self, tmp_path, monkeypatch):
        cfg_file = _write_config(
            tmp_path,
            'worktree_base = "/tmp/wt"\ngithub_user = "alice"\nbackend = "devin"\n'
            'branch_prefix = "usr/test"\n'
            '[[repo]]\nname = "owner/repo"\npredd = true\nhunter = true\nobsidian = false\n',
        )
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        from click.testing import CliRunner
        runner = CliRunner()
        runner.invoke(pw.cli, ["config", "set", "backend", "claude"])
        import tomllib
        with open(cfg_file, "rb") as f:
            data = tomllib.load(f)
        # Other fields preserved
        assert data["github_user"] == "alice"
        assert data["branch_prefix"] == "usr/test"
        assert data["backend"] == "claude"


class TestInitCommand:
    def test_ui_flag_prints_not_implemented(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pw, "CONFIG_FILE", tmp_path / "config.toml")
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(pw.cli, ["init", "--ui"])
        assert result.exit_code == 0
        assert "not yet implemented" in result.output.lower()

    def test_wizard_writes_config(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.toml"
        monkeypatch.setattr(pw, "CONFIG_FILE", cfg_file)
        monkeypatch.setattr(pw, "CONFIG_DIR", tmp_path)

        # Stub out subprocess.run so gh checks pass silently
        monkeypatch.setattr(
            pw.subprocess, "run",
            lambda *a, **kw: type("R", (), {"returncode": 0})(),
        )

        from click.testing import CliRunner
        runner = CliRunner()
        # Provide interactive input: github_user, worktree_base, backend, model,
        # advanced(n), jira(n), repo name, predd(y), hunter(y), obsidian(n),
        # add another(blank)
        user_input = "\n".join([
            "alice",          # github_user
            str(tmp_path),    # worktree_base (exists, so no create prompt)
            "devin",          # backend
            "swe-1.6",        # model
            "n",              # advanced?
            "n",              # jira?
            "owner/repo",     # repo name
            "y",              # predd?
            "y",              # hunter?
            "n",              # obsidian?
            "",               # add another repo? (blank = done)
        ]) + "\n"

        result = runner.invoke(pw.cli, ["init"], input=user_input)
        assert result.exit_code == 0, result.output
        assert cfg_file.exists()

        import tomllib
        with open(cfg_file, "rb") as f:
            data = tomllib.load(f)
        assert data["github_user"] == "alice"
        assert data["backend"] == "devin"
        assert len(data["repo"]) == 1
        assert data["repo"][0]["name"] == "owner/repo"


# ---------------------------------------------------------------------------
# TestPreflightDiffCheck
# ---------------------------------------------------------------------------

class TestPreflightDiffCheck:
    """Tests for the pre-flight diff-size check in process_pr."""

    def _make_pr(self, number=1, sha="abc123"):
        return {
            "number": number,
            "title": "Test PR",
            "author": {"login": "other"},
            "headRefOid": sha,
            "headRefName": "feature-branch",
            "baseRefName": "main",
            "isDraft": False,
            "reviewRequests": [],
        }

    def _cfg(self, tmp_path, max_pr_diff_lines=2000):
        cfg = pw.Config({
            "repos": ["owner/repo"],
            "worktree_base": str(tmp_path / "worktrees"),
            "github_user": "testuser",
            "backend": "claude",
            "model": "claude-haiku-4-5",
            "skill_path": str(tmp_path / "SKILL.md"),
            "max_pr_diff_lines": max_pr_diff_lines,
        })
        return cfg

    def _fake_size_result(self, additions, deletions, returncode=0):
        r = MagicMock()
        r.returncode = returncode
        r.stdout = json.dumps({"additions": additions, "deletions": deletions})
        return r

    def test_preflight_skips_oversized_pr(self, tmp_path, monkeypatch):
        """Oversized PR: comment posted, status=rejected, setup_worktree NOT called."""
        monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(pw, "CONFIG_DIR", tmp_path)

        cfg = self._cfg(tmp_path, max_pr_diff_lines=500)
        state = {}
        pr = self._make_pr()

        with patch.object(pw, "gh_run", return_value=self._fake_size_result(300, 300)) as mock_gh_run, \
             patch.object(pw, "gh_pr_comment") as mock_comment, \
             patch.object(pw, "setup_worktree") as mock_worktree, \
             patch.object(pw, "save_state"), \
             patch.object(pw, "log_decision"):
            pw.process_pr(cfg, state, "owner/repo", pr)

        mock_comment.assert_called_once()
        comment_body = mock_comment.call_args[0][2]
        assert "too large" in comment_body.lower() or "600" in comment_body
        mock_worktree.assert_not_called()
        assert state.get("owner/repo#1", {}).get("status") == "rejected"

    def test_preflight_proceeds_when_within_limit(self, tmp_path, monkeypatch):
        """PR within limit: setup_worktree IS called."""
        monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(pw, "CONFIG_DIR", tmp_path)

        cfg = self._cfg(tmp_path, max_pr_diff_lines=2000)
        state = {}
        pr = self._make_pr()
        worktree = tmp_path / "wt"
        worktree.mkdir(parents=True)

        skill = tmp_path / "SKILL.md"
        skill.write_text("Review $ARGUMENTS")
        cfg.skill_path = skill

        with patch.object(pw, "gh_run", return_value=self._fake_size_result(100, 50)), \
             patch.object(pw, "setup_worktree", return_value=worktree) as mock_worktree, \
             patch.object(pw, "run_review", return_value="## Verdict\nAPPROVE"), \
             patch.object(pw, "notify_sound"), \
             patch.object(pw, "notify_toast"), \
             patch.object(pw, "save_state"), \
             patch.object(pw, "log_decision"):
            pw.process_pr(cfg, state, "owner/repo", pr)

        mock_worktree.assert_called_once()

    def test_preflight_proceeds_when_api_fails(self, tmp_path, monkeypatch):
        """API failure during size check: fail-open, setup_worktree IS called."""
        monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(pw, "CONFIG_DIR", tmp_path)

        cfg = self._cfg(tmp_path, max_pr_diff_lines=100)
        state = {}
        pr = self._make_pr()
        worktree = tmp_path / "wt"
        worktree.mkdir(parents=True)

        skill = tmp_path / "SKILL.md"
        skill.write_text("Review $ARGUMENTS")
        cfg.skill_path = skill

        with patch.object(pw, "gh_run", side_effect=RuntimeError("network error")), \
             patch.object(pw, "setup_worktree", return_value=worktree) as mock_worktree, \
             patch.object(pw, "run_review", return_value="APPROVE"), \
             patch.object(pw, "notify_sound"), \
             patch.object(pw, "notify_toast"), \
             patch.object(pw, "save_state"), \
             patch.object(pw, "log_decision"):
            pw.process_pr(cfg, state, "owner/repo", pr)

        mock_worktree.assert_called_once()

    def test_preflight_skips_when_limit_is_zero(self, tmp_path, monkeypatch):
        """max_pr_diff_lines=0 disables the check; gh_run not called for size check."""
        monkeypatch.setattr(pw, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(pw, "CONFIG_DIR", tmp_path)

        cfg = self._cfg(tmp_path, max_pr_diff_lines=0)
        state = {}
        pr = self._make_pr()
        worktree = tmp_path / "wt"
        worktree.mkdir(parents=True)

        skill = tmp_path / "SKILL.md"
        skill.write_text("Review $ARGUMENTS")
        cfg.skill_path = skill

        with patch.object(pw, "gh_run") as mock_gh_run, \
             patch.object(pw, "setup_worktree", return_value=worktree) as mock_worktree, \
             patch.object(pw, "run_review", return_value="APPROVE"), \
             patch.object(pw, "notify_sound"), \
             patch.object(pw, "notify_toast"), \
             patch.object(pw, "save_state"), \
             patch.object(pw, "log_decision"):
            pw.process_pr(cfg, state, "owner/repo", pr)

        # gh_run should not have been called for the size check
        size_check_calls = [
            c for c in mock_gh_run.call_args_list
            if "additions" in str(c)
        ]
        assert len(size_check_calls) == 0
        mock_worktree.assert_called_once()


# ---------------------------------------------------------------------------
# TestSpeckitPhaseII (predd side) — run_speckit_review + process_pr fork
# ---------------------------------------------------------------------------

def _make_speckit_cfg_predd(tmp_path: Path) -> pw.Config:
    prompt_dir = tmp_path / "prompts" / "speckit"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "analyze.md").write_text(
        "analyze {plan_path} {spec_refs_dir} {constitution_path} "
        "{capability_spec_path} {story_spec_path} {clarifications_path}"
    )
    (prompt_dir / "tasks.md").write_text(
        "tasks {plan_path} {spec_refs_dir}"
    )
    data = {
        "repos": ["owner/repo"],
        "worktree_base": str(tmp_path / "worktrees"),
        "github_user": "testuser",
        "backend": "claude",
        "model": "claude-opus-4-6",
        "speckit_enabled": True,
        "speckit_run_analyze": True,
        "speckit_prompt_dir": str(prompt_dir),
    }
    import tomllib as _tomllib
    import io as _io
    # Build via raw dict using Config directly
    cfg = pw.Config.__new__(pw.Config)
    cfg.__init__(data)
    return cfg


def _make_speckit_worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "plan.md").write_text("# Plan\n- do something")
    spec_refs = wt / "spec-refs"
    spec_refs.mkdir()
    (spec_refs / "constitution.md").write_text("# Constitution")
    (spec_refs / "capability-spec.md").write_text("# Capability")
    (spec_refs / "story-spec.md").write_text("# Story")
    return wt


class TestRunSpeckitReview:
    """run_speckit_review — approve path, inconsistent path."""

    def test_approve_path_commits_tasks_and_posts_approve(self, tmp_path):
        cfg = _make_speckit_cfg_predd(tmp_path)
        worktree = _make_speckit_worktree(tmp_path)

        with patch.object(pw, "_run_skill_prompt",
                          side_effect=["APPROVE\nLooks good.", "## Tasks\n- task 1"]) as mock_run, \
             patch.object(pw, "gh_pr_review") as mock_review, \
             patch.object(pw, "log_decision"), \
             patch("subprocess.run") as mock_sub:
            pw.run_speckit_review(cfg, "owner/repo", 42, "feat/my-branch",
                                  "<!-- hunter:issue-10 -->", worktree)

        mock_review.assert_called_once()
        review_args = mock_review.call_args[0]
        assert review_args[2] == "approve"
        # tasks.md should have been written
        assert (worktree / "tasks.md").exists()
        assert (worktree / "tasks.md").read_text() == "## Tasks\n- task 1"
        # git add + commit + push called
        sub_cmds = [" ".join(str(a) for a in c[0][0]) for c in mock_sub.call_args_list]
        assert any("git add tasks.md" in c for c in sub_cmds)
        assert any("git commit" in c for c in sub_cmds)
        assert any("git push" in c and "HEAD:feat/my-branch" in c for c in sub_cmds)

    def test_inconsistent_path_posts_request_changes_and_labels_issue(self, tmp_path):
        cfg = _make_speckit_cfg_predd(tmp_path)
        worktree = _make_speckit_worktree(tmp_path)
        pr_body = "Implementation for issue #7\n<!-- hunter:issue-7 -->"

        with patch.object(pw, "_run_skill_prompt",
                          return_value="INCONSISTENT: missing acceptance criteria") as mock_run, \
             patch.object(pw, "gh_pr_review") as mock_review, \
             patch.object(pw, "gh_ensure_label_exists") as mock_ensure, \
             patch.object(pw, "gh_issue_add_label") as mock_label, \
             patch.object(pw, "log_decision"):
            pw.run_speckit_review(cfg, "owner/repo", 42, "feat/my-branch",
                                  pr_body, worktree)

        mock_review.assert_called_once()
        assert mock_review.call_args[0][2] == "request-changes"
        mock_ensure.assert_called_once_with("owner/repo", "needs-replan", color="b60205")
        mock_label.assert_called_once_with("owner/repo", 7, "needs-replan")

    def test_inconsistent_no_issue_number_skips_label(self, tmp_path):
        cfg = _make_speckit_cfg_predd(tmp_path)
        worktree = _make_speckit_worktree(tmp_path)

        with patch.object(pw, "_run_skill_prompt",
                          return_value="INCONSISTENT: something wrong"), \
             patch.object(pw, "gh_pr_review"), \
             patch.object(pw, "gh_issue_add_label") as mock_label, \
             patch.object(pw, "gh_ensure_label_exists") as mock_ensure, \
             patch.object(pw, "log_decision"):
            pw.run_speckit_review(cfg, "owner/repo", 42, "feat/my-branch",
                                  "no issue marker here", worktree)

        mock_label.assert_not_called()
        mock_ensure.assert_not_called()

    def test_missing_plan_raises(self, tmp_path):
        cfg = _make_speckit_cfg_predd(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "spec-refs").mkdir()
        # plan.md absent

        with pytest.raises(RuntimeError, match="plan.md"):
            pw.run_speckit_review(cfg, "owner/repo", 1, "branch", "", worktree)

    def test_missing_spec_refs_raises(self, tmp_path):
        cfg = _make_speckit_cfg_predd(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "plan.md").write_text("plan")
        # spec-refs/ absent

        with pytest.raises(RuntimeError, match="spec-refs"):
            pw.run_speckit_review(cfg, "owner/repo", 1, "branch", "", worktree)

    def test_clarifications_absent_uses_placeholder(self, tmp_path):
        cfg = _make_speckit_cfg_predd(tmp_path)
        worktree = _make_speckit_worktree(tmp_path)
        # no clarifications.md in spec-refs

        captured_prompts = []

        def fake_run(c, prompt, wt):
            captured_prompts.append(prompt)
            return "APPROVE\nok"

        with patch.object(pw, "_run_skill_prompt", side_effect=fake_run), \
             patch.object(pw, "gh_pr_review"), \
             patch.object(pw, "log_decision"), \
             patch("subprocess.run"):
            pw.run_speckit_review(cfg, "owner/repo", 1, "branch", "", worktree)

        assert "(not present)" in captured_prompts[0]

    def test_clarifications_present_uses_real_path(self, tmp_path):
        cfg = _make_speckit_cfg_predd(tmp_path)
        worktree = _make_speckit_worktree(tmp_path)
        clarif = worktree / "spec-refs" / "clarifications.md"
        clarif.write_text("Q: ...")

        captured_prompts = []

        def fake_run(c, prompt, wt):
            captured_prompts.append(prompt)
            return "APPROVE\nok"

        with patch.object(pw, "_run_skill_prompt", side_effect=fake_run), \
             patch.object(pw, "gh_pr_review"), \
             patch.object(pw, "log_decision"), \
             patch("subprocess.run"):
            pw.run_speckit_review(cfg, "owner/repo", 1, "branch", "", worktree)

        assert str(clarif) in captured_prompts[0]


class TestProcessPrSpeckitFork:
    """process_pr calls run_speckit_review for sdd-proposal PRs when speckit_run_analyze=True."""

    def _pr(self):
        return {
            "number": 5,
            "headRefOid": "abc123",
            "headRefName": "feat/my-plan",
            "title": "Plan: my feature",
            "author": {"login": "someone"},
        }

    def test_speckit_proposal_pr_calls_run_speckit_review(self, tmp_path):
        cfg = _make_speckit_cfg_predd(tmp_path)

        pr_detail_response = MagicMock()
        pr_detail_response.stdout = json.dumps({
            "labels": [{"name": "sdd-proposal"}],
            "body": "<!-- hunter:issue-10 -->",
        })

        with patch.object(pw, "gh_run", return_value=pr_detail_response), \
             patch.object(pw, "setup_worktree", return_value=tmp_path / "wt"), \
             patch.object(pw, "run_speckit_review") as mock_speckit, \
             patch.object(pw, "run_review") as mock_review, \
             patch.object(pw, "update_pr_state"), \
             patch.object(pw, "notify_sound"), \
             patch.object(pw, "notify_toast"), \
             patch.object(pw, "save_state"), \
             patch.object(pw, "log_decision"):
            pw.process_pr(cfg, {}, "owner/repo", self._pr())

        mock_speckit.assert_called_once()
        mock_review.assert_not_called()

    def test_non_proposal_pr_calls_run_review(self, tmp_path):
        cfg = _make_speckit_cfg_predd(tmp_path)

        pr_detail_response = MagicMock()
        pr_detail_response.stdout = json.dumps({
            "labels": [{"name": "sdd-implementation"}],
            "body": "some body",
        })
        review_response = MagicMock()
        review_response.stdout = ""

        with patch.object(pw, "gh_run", side_effect=[pr_detail_response]) as mock_gh, \
             patch.object(pw, "setup_worktree", return_value=tmp_path / "wt"), \
             patch.object(pw, "run_speckit_review") as mock_speckit, \
             patch.object(pw, "run_review", return_value="APPROVE") as mock_review, \
             patch.object(pw, "update_pr_state"), \
             patch.object(pw, "notify_sound"), \
             patch.object(pw, "notify_toast"), \
             patch.object(pw, "save_state"), \
             patch.object(pw, "log_decision"), \
             patch.object(pw, "gh_pr_review"):
            pw.process_pr(cfg, {}, "owner/repo", self._pr())

        mock_speckit.assert_not_called()
        mock_review.assert_called_once()

    def test_speckit_review_failure_falls_back_to_run_review(self, tmp_path):
        cfg = _make_speckit_cfg_predd(tmp_path)

        pr_detail_response = MagicMock()
        pr_detail_response.stdout = json.dumps({
            "labels": [{"name": "sdd-proposal"}],
            "body": "<!-- hunter:issue-10 -->",
        })

        with patch.object(pw, "gh_run", return_value=pr_detail_response), \
             patch.object(pw, "setup_worktree", return_value=tmp_path / "wt"), \
             patch.object(pw, "run_speckit_review",
                          side_effect=RuntimeError("plan.md not found")), \
             patch.object(pw, "run_review", return_value="COMMENT general") as mock_review, \
             patch.object(pw, "update_pr_state"), \
             patch.object(pw, "notify_sound"), \
             patch.object(pw, "notify_toast"), \
             patch.object(pw, "save_state"), \
             patch.object(pw, "log_decision"), \
             patch.object(pw, "gh_pr_review"):
            pw.process_pr(cfg, {}, "owner/repo", self._pr())

        mock_review.assert_called_once()


class TestParseIssueNumberFromPrBody:
    def test_hunter_marker(self):
        assert pw._parse_issue_number_from_pr_body("<!-- hunter:issue-42 -->") == 42

    def test_hunter_marker_with_spaces(self):
        assert pw._parse_issue_number_from_pr_body("<!-- hunter:issue-7 -->") == 7

    def test_fallback_issue_text(self):
        assert pw._parse_issue_number_from_pr_body("Implementation for issue #99") == 99

    def test_hunter_marker_preferred_over_text(self):
        body = "issue #5\n<!-- hunter:issue-15 -->"
        assert pw._parse_issue_number_from_pr_body(body) == 15

    def test_returns_none_when_no_marker(self):
        assert pw._parse_issue_number_from_pr_body("nothing here") is None

    def test_returns_none_for_empty(self):
        assert pw._parse_issue_number_from_pr_body("") is None
