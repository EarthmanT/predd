# Skill Improvements from Conversation Audit

## Problem

Auditing the conversation history reveals six recurring process failures when agents run the skills via predd/hunter. These are not code bugs — they're gaps in the skill prompts that let agents make avoidable mistakes.

### 1. Skills assume interactive mode but run headless

All three skills reference `AskUserQuestion` for clarification. When predd/hunter invoke them headlessly via `claude -p` or Bedrock, there is no user to ask. The agent either hangs waiting for input, silently skips the step, or hallucinates an answer. This caused the status page to ship with 10 self-admitted problems — the agent never paused to validate because it couldn't ask.

### 2. Proposal skill doesn't enforce spec-first

The sdd-proposal skill creates `proposal.md`, `design.md`, and `tasks.md` via openspec — but when hunter passes a GitHub issue as context, the agent sometimes skips the openspec workflow entirely and starts writing code directly. This happened twice in the audited conversation: once for bedrock, once for the obsidian output path fix. The skill prompt does not say "do not write code."

### 3. Implementation skill has no self-review gate

The sdd-implementation skill says "implement tasks until done or blocked" but has no step that says "verify your changes work before declaring done." The agent shipped a status page with broken click handlers, then shipped a rebuild with broken click handlers again. Both times it declared "done" without testing.

### 4. PR review skill posts COMMENT but predd expects verdict extraction

The pr-review skill posts reviews as `event: "COMMENT"` (non-blocking). Predd's `process_pr()` then scans the review text for `APPROVE`, `REQUEST_CHANGES`, or `COMMENT` strings to extract a verdict for the decision log. This is fragile — the verdict is buried in free text and the extraction is a substring match that can false-positive on quoted text or descriptions.

### 5. No structured final output for programmatic routing

All three skills produce free-text output. When run headlessly, the calling daemon (predd/hunter) has to parse this free text to determine what happened. This caused:
- Predd couldn't reliably extract verdicts
- Hunter couldn't tell if a proposal was complete vs. partial
- No machine-readable signal for "I need human help"

### 6. Implementation skill doesn't commit with meaningful messages

The implementation skill says "make the code changes required" but doesn't specify commit discipline. Hunter's `commit_skill_output()` wraps everything in a single commit with a generic message. The skill should instruct the agent to commit incrementally with descriptive messages as it completes each task.

## Solution

### A. Add headless mode instructions to all three skills

Add a section to each skill:

```markdown
## Headless Mode

When running without an interactive user (no terminal, invoked via `claude -p` or API):
- Do NOT use AskUserQuestion — there is no user to ask
- Make reasonable decisions and document assumptions in your output
- If a task is critically unclear, skip it and note it as blocked in your output
- Always complete with a structured JSON summary (see Output section)
```

### B. Add "no code" guardrail to proposal skill

Add to the sdd-proposal SKILL.md guardrails:

```markdown
- Do NOT write, modify, or delete any source code files
- Do NOT run implementation commands (npm install, pip install, make, etc.)
- Your output is ONLY spec artifacts: proposal.md, design.md, tasks.md
- If the issue description is a spec file (markdown with ## Problem / ## Solution sections), use it as the basis for your proposal rather than starting from scratch
```

### C. Add verification step to implementation skill

Add after step 6 (implement tasks):

```markdown
6b. **Verify changes**

After completing all tasks:
- Run the project's test suite if one exists (check for pytest, npm test, make test)
- If tests fail, fix the failures before proceeding
- If no test suite exists, manually verify key functionality works:
  - Read back modified files and check for syntax errors
  - Verify imports resolve
  - Check that new functions are called from somewhere
- Do NOT declare completion until verification passes
```

### D. Require structured JSON epilogue from all skills

Add to all three skills' output sections:

```markdown
## Structured Output

After all other output, emit a JSON block on its own line, fenced with ```json:

For pr-review:
{
  "verdict": "APPROVE" | "REQUEST_CHANGES" | "COMMENT",
  "findings_count": <int>,
  "critical_count": <int>,
  "files_reviewed": [<paths>]
}

For sdd-proposal:
{
  "status": "complete" | "partial" | "blocked",
  "artifacts_created": [<filenames>],
  "blocked_reason": <string or null>,
  "assumptions": [<strings>]
}

For sdd-implementation:
{
  "status": "complete" | "partial" | "blocked",
  "tasks_completed": <int>,
  "tasks_total": <int>,
  "tests_passed": <bool or null>,
  "files_changed": [<paths>],
  "blocked_reason": <string or null>
}
```

### E. Add commit discipline to implementation skill

Add to implementation skill guardrails:

```markdown
- Commit after completing each logical task, not all at once
- Use descriptive commit messages: "feat: <what changed>" or "fix: <what was fixed>"
- Do NOT bundle all changes into a single commit
- Do NOT use generic messages like "implement tasks" or "make changes"
```

## Files Touched

- `.skills/sdd-proposal/SKILL.md` — add headless mode, no-code guardrail, structured output
- `.skills/sdd-implementation/SKILL.md` — add headless mode, verification step, structured output, commit discipline
- `.skills/pr-review/SKILL.md` — add headless mode, structured JSON epilogue

## Testing

- Run `predd start --once` against a repo with an open PR — verify the review output ends with a JSON block containing `verdict`
- Run `hunter start --once` against a repo with an assigned issue — verify the proposal output ends with a JSON block containing `status`
- Verify no `AskUserQuestion` calls appear in the daemon logs when running headlessly
- Verify implementation skill runs tests before declaring completion

## Out of Scope

- Changing how predd/hunter parse skill output (that's a separate spec after structured output lands)
- Adding new tools to the bedrock agent loop (grep, read_file with ranges, etc.)
- Prompt caching for skills (separate optimization spec)
