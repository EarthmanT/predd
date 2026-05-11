# Fix: Claude Backend Tool Use in Non-Interactive Mode

## Problem

`claude -p` (print mode) does not execute tool calls by default — it requires explicit permission. When predd runs the pr-review skill via `claude -p`, Claude refuses to run `gh api`, `git`, or any bash commands, returning a message like:

> "I'm unable to proceed... The system is running in non-interactive mode and requires explicit permission to execute shell commands."

This means the skill runs but never actually posts the review to GitHub. The review summary is saved locally but no GitHub review is created.

## Fix

Add `--dangerously-skip-permissions` to the `claude -p` invocation:

```python
["claude", "-p", "--dangerously-skip-permissions", "--model", cfg.model, prompt]
```

This allows Claude to execute tool calls (Bash, gh, git) without prompting in non-interactive mode. Appropriate here because predd runs in a controlled, trusted local environment.

## Already Fixed

Implemented in `predd.py` `_run_claude()`.
