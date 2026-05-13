# The `analyze` Command

## Problem

Logs and feedback accumulate but nobody reads them. The improvement loop needs a trigger that says: "look at what's been happening and produce actionable improvements."

## Proposed Behaviour

Two new CLI commands:

```bash
predd analyze [--days 7]    # Analyze predd review patterns
hunter analyze [--days 7]   # Analyze hunter proposal/impl patterns
```

Each command:
1. Reads the last N days of decision log + PR feedback
2. Sends it to Claude with an analysis prompt
3. Outputs findings to stdout
4. Optionally writes a spec file to `spec/changes/` if actionable improvements are found

## Analysis Prompts

### predd analyze

> "Here is a structured log of PR review decisions over the last {N} days. Each line is a JSON event. Identify:
> 1. PRs that were consistently skipped that probably shouldn't have been
> 2. Reviews that got REQUEST_CHANGES from the author — what did predd miss?
> 3. Any patterns in what predd flags vs what humans care about
>
> Output findings as a markdown list. If you identify a concrete improvement to the review skill or predd logic, write it as a spec in the format used in spec/changes/."

### hunter analyze

> "Here is a structured log of hunter issue processing over the last {N} days, including PR feedback from humans. Identify:
> 1. Issues where the proposal was rejected or heavily revised — what was wrong?
> 2. Issues where the impl PR had REQUEST_CHANGES — what patterns recur?
> 3. Cases where hunter failed or got stuck — what caused it?
> 4. Suggestions for improving the proposal skill, impl skill, or hunter logic
>
> Output findings as a markdown report. For each concrete improvement, write a spec."

## Output

```
$ hunter analyze --days 14

## Hunter Analysis — last 14 days

**Issues processed:** 23
**Proposals merged without changes:** 14 (61%)
**Proposals with REQUEST_CHANGES:** 7 (30%)
**Proposals closed without merging:** 2 (9%)

### Patterns

1. **Missing error handling in proposals** (5/7 REQUEST_CHANGES)
   The proposal skill rarely includes error handling approach in the design section.
   → Wrote spec: spec/changes/improve-proposal-error-handling.md

2. **Impl PRs missing tests** (4/7 REQUEST_CHANGES)
   Implementation PRs frequently omit test coverage for new functions.
   → Wrote spec: spec/changes/improve-impl-test-coverage.md
```

## Implementation Notes

- Reads JSONL files and filters by timestamp
- Chunks large logs if needed (>100 events → summarize in batches)
- Uses `claude -p` with `--dangerously-skip-permissions` and `stdin_text`
- If `--write-specs` flag: Claude is instructed to write spec files directly via tool calls
- Without flag: analysis is printed only, no files written

## Config

```toml
analyze_model = "claude-opus-4-7"  # Use a smarter model for analysis
```
