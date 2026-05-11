# Hunter: Pass Full Issue Context to Skills

## Problem

Hunter passes only the issue number as `$ARGUMENTS` to the proposal and impl skills:

```python
run_skill(cfg, cfg.proposal_skill_path, str(issue_number), worktree)
```

The openspec proposal skill uses `$ARGUMENTS` to understand what to build. When it receives just `344`, it either:
- Tries to look up the issue itself (requires interactive `gh` auth in `-p` mode)
- Falls back to `AskUserQuestion` to ask what the user wants to build — which silently fails in non-interactive mode

Result: the skill runs, creates nothing, and hunter marks the issue `failed`.

## Proposed Fix

Build a rich context string from the GH issue and pass it as `$ARGUMENTS`. The skill receives everything it needs to proceed without interaction.

## Context Format

```
Issue #344: [DAP09A-1184] TOON - Tool to Reduce Token Usage in DAPO MCP - POC

Type: Story
Epic: DAP09A-1100
Sprint: DAP09A Sprint-10 2026-05-12
Capability: 1234 — token optimization

Description:
<full issue body text>
```

## Implementation

Replace the `run_skill` call in `process_issue` and `check_proposal_merged`:

```python
def build_issue_context(issue_number: int, title: str, body: str, entry: dict) -> str:
    lines = [f"Issue #{issue_number}: {title}", ""]
    for field in ("Type", "Epic", "Sprint", "Capability"):
        val = entry.get(field.lower())
        if val:
            lines.append(f"{field}: {val}")
    lines += ["", "Description:", body or "(no description)"]
    return "\n".join(lines)
```

The `body` comes from `gh issue view --json body` fetched when hunter first picks up the issue. Store it in hunter state at pickup time.

## Where `$ARGUMENTS` Goes

The context string replaces `$ARGUMENTS` in the skill prompt verbatim. The openspec skill reads `$ARGUMENTS` as the user's description of what to build — a multi-line string works fine.

## Scope

- `process_issue`: fetch and store issue body at pickup, pass context to proposal skill
- `check_proposal_merged`: read stored body from state, pass context to impl skill
- No change to skill files
