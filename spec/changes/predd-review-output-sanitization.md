# predd: review output sanitization

## Problem

predd posts whatever the skill subprocess returns as the GitHub review body, verbatim. When the model leaks its internal monologue into the output — preamble like "Perfect! The review has been posted. Now let me print the terminal summary:" — that text lands directly on the PR.

Observed in fusion-e/ai-bp-toolkit#385 (review ID 4296012254):
- Review body opened with `"Perfect! The review has been posted. Now let me print the terminal summary:"`
- Included a raw code block with terminal-style output
- Included a raw JSON block (the structured verdict)
- Followed by a duplicate prose summary

The actual findings were correct, but the wrapping was the model's internal reasoning, not the review content.

## Root cause

The skill is structured to emit a JSON block (the verdict) and a markdown summary. predd passes the full stdout of the skill subprocess to GitHub with no post-processing. There is no output parser.

## What to fix

Parse the skill output before posting. The review skill emits a JSON block of the form:

```json
{
  "verdict": "REQUEST_CHANGES",
  "findings_count": 6,
  ...
}
```

followed by a markdown summary. Extract the verdict from the JSON block and use the markdown that follows it as the review body. Discard anything before the first `##` heading (the preamble / internal monologue).

### Extraction rules

1. Find the last fenced ```json block in the output — extract `verdict` from it
2. Find the first `## ` heading in the output — use everything from there to end of output as the review body
3. If no JSON block found: fall back to full output, log a warning
4. If no `## ` heading found: use full output, log a warning
5. Strip trailing whitespace from the extracted body

### Verdict mapping

| JSON `verdict` | GitHub review event |
|----------------|---------------------|
| `APPROVE` | `APPROVE` |
| `REQUEST_CHANGES` | `REQUEST_CHANGES` |
| `COMMENT` | `COMMENT` |
| (missing / unparseable) | `COMMENT` (safe default) |

## What not to change

- The skill files themselves — the fix is in predd's output handling, not the prompts
- The decision log schema — `pr_review_posted` already records the verdict string

## Tests

- Output with preamble + JSON block + markdown body → preamble stripped, verdict extracted, body starts at `##`
- Output with no JSON block → full output used, warning logged
- Output with no `##` heading → full output used, warning logged
- JSON verdict `APPROVE` / `REQUEST_CHANGES` / `COMMENT` → correct GitHub event
- Malformed JSON block → falls back to `COMMENT` verdict, logs warning
