# Fix Bedrock Backend: Correctness, Config, and Prompt Caching

## Problem

The Bedrock backend, which the README now recommends as the default, is broken in three ways and missing the single biggest cost optimization the SDK supports.

### 1. `AnthropicBedrock` constructed with invalid kwargs

`predd.py:1615` calls:

```python
client = AnthropicBedrock(
    aws_profile=cfg.aws_profile,
    region_name=cfg.aws_region,
)
```

Neither kwarg is valid. The SDK's `AnthropicBedrock` accepts `aws_region`, `aws_access_key`, `aws_secret_key`, `aws_session_token` — not `region_name` (that's a boto3 convention) and not `aws_profile` (the SDK doesn't read profiles directly). Either the call TypeErrors on construction or the kwargs are silently dropped depending on SDK version, leaving the client pointed at whatever region `AWS_REGION` or the default chain resolves. Either way, `backend = "bedrock"` does not work as documented.

### 2. `cfg.analyze_hour` referenced but never defined

`obsidian.py:774` reads `cfg.analyze_hour` inside the daemon loop:

```python
if last_analyze_date != today and current_hour >= cfg.analyze_hour:
```

`Config` (`predd.py:217–289`) defines `analyze_days`, `analyze_interval`, and `analyze_model`, but not `analyze_hour`. `obsidian start` raises `AttributeError` on the first poll. The `obsidian observe` and `obsidian analyze` subcommands work — only the daemon mode is broken.

### 3. No prompt caching on the Bedrock client

`_run_bedrock_skill` (`predd.py:1604–1677`) loads SKILL.md into `system` as a plain string on every turn of every run. With `MAX_TURNS = 50` and the obsidian → spec → hunter loop running daily, the same SKILL.md text is re-sent to Bedrock thousands of times. Bedrock supports prompt caching for the AnthropicBedrock client today (`cache_control: {"type": "ephemeral"}`); the README labels this as "future" but it is available now.

## Proposed Behaviour

### 1. Correct Bedrock client construction

Replace the construction block in `_run_bedrock_skill`:

```python
import os
from anthropic import AnthropicBedrock

# Set AWS_PROFILE in env if configured; the underlying boto3 client
# AnthropicBedrock uses will pick it up.
if cfg.aws_profile and cfg.aws_profile != "default":
    os.environ["AWS_PROFILE"] = cfg.aws_profile

client = AnthropicBedrock(aws_region=cfg.aws_region)
```

Leave `aws_access_key` / `aws_secret_key` / `aws_session_token` unset — the default AWS credential chain (env vars, `~/.aws/credentials`, SSO, IAM role) covers every real deployment. Document this in the config comment.

### 2. Add `analyze_hour` to `Config`

Add to `Config.__init__` in `predd.py`:

```python
self.analyze_hour: int = data.get("analyze_hour", 9)
```

Add to `Config.to_dict()`:

```python
"analyze_hour": self.analyze_hour,
```

Add to `DEFAULT_CONFIG_TEMPLATE` in `predd.py`:

```toml
# Hour of the day (0-23, local time) to run obsidian analyze
analyze_hour = 9
```

No other behaviour change — `obsidian.py:774` already reads it correctly.

### 3. Enable prompt caching on the Bedrock backend

Change the `system` argument in `_run_bedrock_skill` from a string to a list of content blocks with cache_control on the SKILL.md block:

```python
system = [
    {
        "type": "text",
        "text": (
            "You are an AI engineer assistant. "
            "Follow the SKILL.md to complete the task."
        ),
    },
    {
        "type": "text",
        "text": f"--- SKILL.md ---\n{skill_text}\n--- end SKILL.md ---",
        "cache_control": {"type": "ephemeral"},
    },
]
```

The cached block must be at least 1024 tokens for Sonnet to actually hit the cache. SKILL.md files in this project comfortably clear that threshold; no padding needed. Cache TTL is 5 minutes (ephemeral), which fits the agentic loop (50 turns finish well inside that window) and the polling cadence.

Expected effect: ~90% reduction in input-token cost on cached turns, ~30–50% latency reduction on turns 2+.

## Out of scope

- Token usage logging / cost dashboards — separate spec
- Streaming, retries, backoff — separate spec
- Tool result caching — different mechanism, not worth it for this workload
- Switching `obsidian.py` to use the Bedrock backend (currently `_run_claude` only) — separate spec; the analyze model and the runtime backend are intentionally decoupled

## Acceptance Criteria

1. `backend = "bedrock"` end-to-end smoke test:
   - `AnthropicBedrock` constructs without TypeError using only `aws_region`
   - `AWS_PROFILE` env var is set when `cfg.aws_profile != "default"`
   - A single PR review completes via the Bedrock backend without crash
2. `obsidian start` runs at least one poll cycle without AttributeError
3. `Config.to_dict()` round-trips `analyze_hour`; CLI `predd config` shows the value
4. A unit test in `test_pr_watcher.py` mocks `AnthropicBedrock` and asserts:
   - It is called with `aws_region=` and not `region_name=` or `aws_profile=`
   - The `system` argument passed to `messages.create` is a list of two blocks
   - The SKILL.md block carries `cache_control == {"type": "ephemeral"}`
5. Existing tests pass: `uv run --with pytest pytest test_pr_watcher.py test_hunter.py test_obsidian.py -q`

## Files Touched

- `predd.py` — Config (`analyze_hour`), DEFAULT_CONFIG_TEMPLATE, `_run_bedrock_skill`
- `test_pr_watcher.py` — new bedrock construction + cache control test

No changes to `hunter.py` or `obsidian.py` required; both consume the fixes transparently.
