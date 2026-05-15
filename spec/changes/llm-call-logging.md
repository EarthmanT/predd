# LLM call logging — count, input chars, output chars

## Problem

There is no visibility into how often the LLM is being called, how much input is being sent, or how much output is returned. This makes it impossible to reason about cost, latency, or whether prompts are growing unexpectedly large.

## What to fix

Log a structured entry every time an LLM call is made (Bedrock, Claude CLI, or Devin), immediately before the call and immediately after it completes.

### Before call (INFO)

```
INFO llm_call_start backend=bedrock model=... context=pr_review pr=442 input_chars=14523
```

### After call (INFO)

```
INFO llm_call_end backend=bedrock model=... context=pr_review pr=442 input_chars=14523 output_chars=3201 duration_s=8.4
```

### Decision log entry

Also write a `llm_call` event to the decision log so it can be queried and aggregated:

```json
{
  "ts": "...",
  "event": "llm_call",
  "backend": "bedrock",
  "model": "...",
  "context": "pr_review",
  "pr": 442,
  "input_chars": 14523,
  "output_chars": 3201,
  "duration_s": 8.4
}
```

### Context labels

Pass a `context` string at each call site so logs are meaningful:

| Call site | context |
|-----------|---------|
| PR review skill | `pr_review` |
| Proposal skill | `proposal` |
| Implementation skill | `impl` |
| Self-review skill | `self_review` |
| Obsidian observe | `observe` |
| Obsidian analyze | `analyze` |
| Intake capability constitution | `intake_constitution` |
| Intake capability spec | `intake_spec` |
| Intake story | `intake_story` |

### Where to instrument

All LLM calls route through one of three functions in `predd.py`:
- `_run_bedrock_skill` — Bedrock
- `_run_proc` (claude backend) — Claude CLI subprocess
- `_run_proc` (devin backend) — Devin subprocess

Instrument at those three entry points. No need to touch individual call sites.

## What not to change

- Log format for existing events
- Decision log schema for other events

## 24-hour summary log

At the start of each daemon poll cycle, if 24 hours have elapsed since the last summary, write a summary line to the log:

```
INFO llm_daily_summary period=24h calls=47 input_chars=612400 output_chars=84200 backends=bedrock:45,claude:2
```

Also write a `llm_daily_summary` decision log entry:

```json
{
  "ts": "...",
  "event": "llm_daily_summary",
  "period_hours": 24,
  "calls": 47,
  "input_chars": 612400,
  "output_chars": 84200,
  "by_context": {
    "pr_review": {"calls": 32, "input_chars": 410000, "output_chars": 61000},
    "proposal": {"calls": 8, "input_chars": 120000, "output_chars": 15000}
  }
}
```

Aggregate from the `llm_call` entries in the decision log for the prior 24 hours. Written by both predd and hunter (each to their own decision log).

## Tests

- `_run_bedrock_skill` call → `llm_call` decision log entry with correct input/output chars and duration
- `_run_proc` (claude) → same
- Exception during call → `llm_call` entry still written with `error=true`, no output_chars
