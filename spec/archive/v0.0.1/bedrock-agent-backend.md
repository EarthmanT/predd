# Bedrock Agent Backend

## Summary
Add AWS Bedrock (Claude via managed service) as an alternative backend for predd and hunter, using agentic tool use pattern.

## Problem
- Current backends: Devin (rate-limited) or Claude CLI (local)
- Need enterprise backend option with tool use capabilities
- Bedrock provides managed Claude with full agentic loop support

## Solution
Add `backend = "bedrock"` option that:
1. Uses `anthropic[bedrock]` SDK (AnthropicBedrock client)
2. Runs agentic loop with tool use (read_file, list_files, bash)
3. Configurable AWS credentials and region
4. Supports any Bedrock Claude model

## Configuration
```toml
# Backend selection
backend = "bedrock"  # "devin" | "claude" | "bedrock"

# Bedrock-specific config
aws_profile = "cloudify-developers"    # AWS profile name
aws_region = "eu-west-1"               # AWS region
bedrock_model = "eu.anthropic.claude-3-7-sonnet-20250219-v1:0"
```

## Implementation Details

### Config Class Changes
```python
class Config:
    aws_profile: str = "default"
    aws_region: str = "us-east-1"
    bedrock_model: str = "eu.anthropic.claude-3-7-sonnet-20250219-v1:0"
```

### New Function: `_run_bedrock_skill()`
Location: `predd.py`

```python
def _run_bedrock_skill(cfg: Config, prompt: str, skill_path: Path, worktree: Path) -> str:
    """Run skill via Bedrock Claude with agentic tool use."""
    from anthropic import AnthropicBedrock

    # Load SKILL.md
    skill_text = skill_path.read_text()

    # Create Bedrock client (uses AWS credential chain + profile)
    client = AnthropicBedrock(
        aws_profile=cfg.aws_profile,
        aws_region=cfg.aws_region
    )

    # Build system prompt
    system = f"""You are an AI engineer. Follow the SKILL.md to complete the task.

--- SKILL.md ---
{skill_text}
--- end SKILL.md ---"""

    # Run agentic loop (up to MAX_TURNS)
    messages = [{"role": "user", "content": prompt}]

    for turn in range(50):  # MAX_TURNS
        resp = client.messages.create(
            model=cfg.bedrock_model,
            max_tokens=4096,
            system=system,
            tools=BEDROCK_TOOLS,  # read_file, list_files, bash
            messages=messages
        )

        # Collect output
        output = ""
        for block in resp.content:
            if block.type == "text":
                output += block.text

        # If no tool use, we're done
        if resp.stop_reason != "tool_use":
            return output

        # Process tool calls
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = _handle_bedrock_tool(block.name, block.input, worktree)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        messages.append({"role": "assistant", "content": [...]})
        messages.append({"role": "user", "content": tool_results})

    return output
```

### Tool Definitions
```python
BEDROCK_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a text file in the worktree",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to worktree)"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "bash",
        "description": "Run bash command in worktree",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": "Seconds (default 120)"}
            },
            "required": ["command"]
        }
    }
]
```

### Usage in predd.py and hunter.py
```python
def _run_claude(cfg: Config, prompt: str, skill_path: Path, worktree: Path) -> str:
    if cfg.backend == "bedrock":
        return _run_bedrock_skill(cfg, prompt, skill_path, worktree)
    # ... existing claude/devin logic
```

## Dependencies
Add to script dependencies:
```toml
dependencies = [
    "click",
    "anthropic[bedrock]",  # For Bedrock support
]
```

## AWS Credentials
Uses standard credential chain:
1. Environment variables: `AWS_PROFILE`, `AWS_REGION`
2. `~/.aws/credentials` (from specified profile)
3. IAM role (if running on EC2/Lambda)
4. SSO (if configured)

## Benefits
- ✓ Enterprise-grade managed service
- ✓ Full tool use support (bash, read, write)
- ✓ Agentic reasoning loop
- ✓ No rate limiting
- ✓ Regional model endpoints
- ✓ Costs predictable and lower than CLI

## Risks
- Requires AWS account and Bedrock access
- Network dependency (external API calls)
- Bedrock API changes could break integration

## Testing
```bash
# Test bedrock backend
export AWS_PROFILE=cloudify-developers
export AWS_REGION=eu-west-1

# In config.toml
backend = "bedrock"
bedrock_model = "eu.anthropic.claude-3-7-sonnet-20250219-v1:0"

# Run
predd.py start --once
```

## Future
- Support other Bedrock models (Claude 4.6, etc.)
- Add model switching without restart
- Cache credentials in ~/.config/predd/aws/
