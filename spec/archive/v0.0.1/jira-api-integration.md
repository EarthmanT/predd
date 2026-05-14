# Jira API Integration

## Problem

Hunter currently ingests Jira data via CSV exports, which requires manual export steps and produces stale data with duplicate columns and missing fields. A direct Jira REST API integration would eliminate the CSV pipeline entirely, providing real-time data and reliable field access.

## Solution

Add a Jira REST API client to hunter that can:
1. Query issues by project/sprint/epic directly from the Jira API
2. Replace CSV ingest for repos that have Jira API access configured
3. Fall back to CSV ingest when API credentials are not configured

## Connection Details

- **Base URL:** Configured via `jira_base_url` (existing config field)
- **Auth:** OAuth 2.0 Bearer Token via `JIRA_API_TOKEN` environment variable
- **API Version:** REST API v2 (`/rest/api/2/`)

**IMPORTANT:** The token MUST be stored in an environment variable (`JIRA_API_TOKEN`), never in config files or code.

## Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /rest/api/2/myself` | Validate authentication |
| `GET /rest/api/2/search?jql=...` | Query issues by project, sprint, epic |
| `GET /rest/api/2/issue/{key}` | Fetch single issue with all fields |
| `GET /rest/api/2/project` | List accessible projects |

## Configuration

New fields in `config.toml`:

```toml
# Jira API integration (optional, replaces CSV ingest when configured)
# jira_api_enabled = true
# jira_base_url = "https://jira.cec.lab.emc.com"  # already exists
# Token is read from JIRA_API_TOKEN env var — do NOT put it in config
```

## Implementation

### JiraClient class (in predd.py, shared)

```python
class JiraClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = None  # lazy urllib.request or subprocess curl

    def search(self, jql: str, fields: list[str], max_results: int = 50) -> list[dict]:
        """Run a JQL query and return issues."""
        ...

    def get_issue(self, key: str) -> dict:
        """Fetch a single issue by key."""
        ...

    def validate(self) -> bool:
        """Test auth via /rest/api/2/myself."""
        ...
```

### Integration with hunter

```python
def ingest_jira_api(cfg: Config, repos: list[str]) -> None:
    """Query Jira API for sprint issues and create missing GH issues."""
    token = os.environ.get("JIRA_API_TOKEN")
    if not token or not cfg.jira_api_enabled:
        return  # fall back to CSV ingest

    client = JiraClient(cfg.jira_base_url, token)
    # Query: project = X AND sprint in openSprints()
    # Apply same filters: skip_jira_issue_types, sprint hard gate
    # Create GH issues same as CSV path
```

### Rate Limiting

- 100 requests/minute (Jira default)
- Respect `retry-after` header on 429 responses
- Use exponential backoff consistent with existing `gh_run` transient error handling

### Fallback Behavior

```
if JIRA_API_TOKEN set and jira_api_enabled:
    use API ingest
elif jira_csv_dir configured:
    use CSV ingest (existing behavior)
else:
    skip Jira ingest entirely
```

## Dependencies

- No new dependencies — use `urllib.request` (stdlib) for HTTP calls
- Token passed via `JIRA_API_TOKEN` env var

## Security

- Token stored in environment variable only
- Never logged, never written to state/decision files
- Mask token in any error messages: `token[:4] + "****"`

## Testing

- Mock HTTP responses for search, get_issue, validate
- Test JQL query construction for sprint/project filters
- Test fallback: API fails gracefully, CSV ingest still works
- Test rate limit retry behavior
- Test skip_jira_issue_types and sprint hard gate apply to API results same as CSV

## Out of Scope

- Jira webhooks (polling only, consistent with predd/hunter design)
- Writing back to Jira (read-only integration)
- Jira Cloud vs Server differences (target Server v2 API only)
