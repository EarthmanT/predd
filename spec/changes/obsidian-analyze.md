# obsidian analyze

## What It Does

Runs daily. Reads observation notes from the last 7 days (configurable). Sends them to Claude/Devin. Produces two outputs:
1. A dated analysis note in `analysis/`
2. Zero or more spec files written to `spec/changes/` for concrete improvements

## Analysis Prompt

```
You are analyzing observations of an AI-powered code review and issue pipeline (predd + hunter).

Here are observation notes from the last {N} days:

{notes}

Identify:
1. Recurring patterns in what human reviewers request that the AI missed
2. Patterns in proposal quality issues (missing sections, wrong scope, vague tasks)
3. Patterns in implementation quality issues (missing tests, incomplete coverage)
4. Cases where hunter got stuck or failed — what caused it?
5. Any hunter/predd logic bugs or edge cases revealed by the observations

For each pattern:
- Describe it clearly
- Estimate how often it occurs (out of N observations)
- Suggest a concrete fix (skill improvement, prompt change, or code change)

If you identify a fix that should be implemented as a code/config change,
write it as a spec file using this format and place it in spec/changes/:
  - Filename: kebab-case description
  - Contents: follow the spec format in spec/changes/ (see existing examples)

Be direct. Prioritize by impact. Skip patterns with only 1 occurrence.
```

## Output Note

Path: `analysis/YYYY-MM-DD.md`

```markdown
---
type: analysis
period: 2026-05-06 to 2026-05-13
observations_analyzed: 23
patterns_found: 4
specs_written: 2
analyzed_at: 2026-05-13T08:00:00Z
---

## Patterns

### 1. Proposals missing error handling (7/23 observations)
...

### 2. Impl PRs missing tests for new functions (5/23 observations)
...

## Specs Written
- [[spec/changes/improve-proposal-error-handling]]
- [[spec/changes/improve-impl-test-coverage]]

## Observations Analyzed
- [[observations/2026-05-13-pr-378]]
- [[observations/2026-05-12-pr-375]]
...
```

## CLI

```bash
obsidian analyze [--days 7] [--model claude-opus-4-7] [--dry-run]
```

`--dry-run` prints the analysis without writing files or specs.

## Config

```toml
analyze_model = "claude-opus-4-7"   # smarter model for analysis
analyze_days = 7
```
