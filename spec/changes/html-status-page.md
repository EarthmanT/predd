# Hunter/Predd: Visual HTML Status Page

## Problem

Currently, checking the status of hunter and predd requires running CLI commands (`hunter status`, `hunter list`, `predd list`) and parsing JSON output. There's no visual overview of what's happening, and the state is scattered across multiple state files and decision logs. This makes it hard to quickly understand:
- What issues/PRs are currently being processed
- What's stuck or failed
- Recent activity patterns
- Overall health of the system

## Proposed Behaviour

Add a local HTTP server that serves a visual HTML status page showing the current state of hunter and predd. The page should be accessible via a web browser and auto-refresh periodically.

### Page Layout

#### Header
- Title: "Hunter & Predd Status"
- Last updated timestamp
- Auto-refresh indicator (e.g., "Refreshing every 30s")
- Config link (optional, to view current config)

#### Hunter Section

**Summary Cards:**
- Total issues in pipeline
- Implementing (with count)
- Proposal Open (with count)
- Failed (with count)
- Submitted (with count)

**Issue Table:**
| Issue | Title | Status | Proposal PR | Impl PR | Age | Actions |
|-------|-------|--------|-------------|---------|------|---------|
| #377 | Port unmerged linter-core PRs | implementing | #378 | #387 | 1d | [GitHub] [Rollback] |
| #319 | ISV Onboarding Workflow | proposal_open | #382 | — | 2d | [GitHub] |
| #63 | BinaryImage artifact property | proposal_open | #384 | — | 3d | [GitHub] |

**Failed Issues (collapsed by default, expandable):**
- Same table structure but filtered to `status == "failed"`
- Shows failure reason if available
- [Retry] button to rollback and retry

#### Predd Section

**Summary Cards:**
- Total PRs tracked
- Reviewing (with count)
- Submitted (with count)
- Failed (with count)

**PR Table:**
| PR | Title | Status | Verdict | Age | Actions |
|----|-------|--------|---------|------|---------|
| #382 | ISV Onboarding Workflow | submitted | APPROVE | 1h | [GitHub] |
| #387 | Port unmerged linter-core | reviewing | — | 2h | [GitHub] |

**Failed PRs (collapsed by default):**
- Same table structure but filtered to `status == "failed"`
- Shows failure reason

#### Recent Activity

**Decision Log Timeline:**
```
11:31 AM - Collected feedback for impl PR #387 (EarthmanT)
11:02 AM - PR #384 approved by Melainbal
10:22 AM - Impl PR #387 created for issue #377
10:22 AM - Proposal PR #378 merged, starting implementation
```

- Last 20 decisions from both hunter-decisions.jsonl and decisions.jsonl
- Color-coded by event type (green for success, red for failure, blue for info)
- Links to relevant GitHub resources where applicable

#### Configuration

**Current Settings:**
- Backend: devin
- Model: swe-1.6
- Repos: fusion-e/ai-bp-toolkit
- Trigger mode: ready
- Max review fix loops: 1

### Visual Design

- Clean, minimal design (no framework required, plain HTML/CSS)
- Status colors:
  - Green: submitted, approved, success
  - Yellow: implementing, proposal_open, reviewing
  - Red: failed
  - Gray: pending, skipped
- Responsive design (works on mobile)
- Dark mode support (auto-detect system preference or toggle)
- Collapsible sections (failed issues, activity log)

## Server Implementation

### HTTP Server

Add a simple HTTP server to hunter.py and predd.py (or a separate `status.py` script):

```python
from http.server import HTTPServer, SimpleHTTPRequestHandler
import json
import threading

class StatusHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = generate_status_html()
            self.wfile.write(html.encode())
        elif self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = get_status_json()
            self.wfile.write(json.dumps(data).encode())
        else:
            self.send_error(404)

def start_status_server(port=8080):
    server = HTTPServer(("localhost", port), StatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
```

### HTML Generation

```python
def generate_status_html() -> str:
    hunter_state = load_hunter_state()
    predd_state = load_state()
    hunter_decisions = load_recent_decisions("hunter-decisions.jsonl", 20)
    predd_decisions = load_recent_decisions("decisions.jsonl", 20)
    cfg = load_config()

    return render_template("status.html", {
        "hunter_state": hunter_state,
        "predd_state": predd_state,
        "hunter_decisions": hunter_decisions,
        "predd_decisions": predd_decisions,
        "config": cfg,
        "updated_at": datetime.now().isoformat(),
    })
```

### JSON API

Provide a JSON endpoint for programmatic access:

```json
{
  "hunter": {
    "summary": {
      "total": 24,
      "implementing": 1,
      "proposal_open": 4,
      "failed": 18,
      "submitted": 1
    },
    "issues": [
      {
        "issue_number": 377,
        "title": "...",
        "status": "implementing",
        "proposal_pr": 378,
        "impl_pr": 387,
        "first_seen": "2026-05-12T21:28:33Z"
      }
    ]
  },
  "predd": {
    "summary": {
      "total": 30,
      "reviewing": 1,
      "submitted": 25,
      "failed": 4
    },
    "prs": [...]
  },
  "recent_activity": [...],
  "config": {...}
}
```

## Config

```toml
# Enable/disable status page server
status_server_enabled = true

# Port for status page (default: 8080)
status_port = 8080

# Auto-refresh interval in seconds (0 = disable)
status_refresh_interval = 30
```

## CLI Commands

Add new commands to start the status server:

```bash
# Start status server (standalone)
predd status-server
hunter status-server

# Or integrate into existing start commands
predd start --status-server
hunter start --status-server
```

## Implementation Notes

### HTML Template

Store HTML template as a multi-line string constant or in a separate `status.html` file:

```html
<!DOCTYPE html>
<html>
<head>
    <title>Hunter & Predd Status</title>
    <style>
        body { font-family: system-ui, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; }
        .summary-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 20px 0; }
        .card { padding: 15px; border-radius: 8px; background: #f5f5f5; }
        .card h3 { margin: 0 0 10px 0; font-size: 14px; color: #666; }
        .card .count { font-size: 32px; font-weight: bold; }
        table { width: 100%; border-collapse: collapse; margin: 20px 0; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }
        .status-implementing { color: #f59e0b; }
        .status-proposal_open { color: #f59e0b; }
        .status-failed { color: #ef4444; }
        .status-submitted { color: #10b981; }
        @media (prefers-color-scheme: dark) {
            body { background: #1a1a1a; color: #e5e5e5; }
            .card { background: #2a2a2a; }
            th, td { border-color: #333; }
        }
    </style>
    <script>
        setTimeout(() => location.reload(), {{ refresh_interval * 1000 }});
    </script>
</head>
<body>
    <h1>Hunter & Predd Status</h1>
    <p>Last updated: {{ updated_at }}</p>

    <h2>Hunter</h2>
    <div class="summary-cards">
        <div class="card"><h3>Total</h3><div class="count">{{ hunter.summary.total }}</div></div>
        <div class="card"><h3>Implementing</h3><div class="count">{{ hunter.summary.implementing }}</div></div>
        <div class="card"><h3>Proposal Open</h3><div class="count">{{ hunter.summary.proposal_open }}</div></div>
        <div class="card"><h3>Failed</h3><div class="count">{{ hunter.summary.failed }}</div></div>
        <div class="card"><h3>Submitted</h3><div class="count">{{ hunter.summary.submitted }}</div></div>
    </div>

    <h3>Issues</h3>
    <table>
        <tr><th>Issue</th><th>Title</th><th>Status</th><th>Proposal PR</th><th>Impl PR</th><th>Age</th></tr>
        {% for issue in hunter.issues %}
        <tr>
            <td><a href="https://github.com/{{ issue.repo }}/issues/{{ issue.issue_number }}">#{{ issue.issue_number }}</a></td>
            <td>{{ issue.title }}</td>
            <td class="status-{{ issue.status }}">{{ issue.status }}</td>
            <td>{% if issue.proposal_pr %}<a href="...">#{{ issue.proposal_pr }}</a>{% endif %}</td>
            <td>{% if issue.impl_pr %}<a href="...">#{{ issue.impl_pr }}</a>{% endif %}</td>
            <td>{{ issue.age }}</td>
        </tr>
        {% endfor %}
    </table>

    <!-- Predd section similar -->
    <!-- Activity timeline -->
</body>
</html>
```

### Template Rendering

Use simple string formatting or a lightweight template approach (no external dependencies):

```python
def render_template(template: str, context: dict) -> str:
    for key, value in context.items():
        template = template.replace("{{ " + key + " }}", str(value))
    # Handle loops with simple regex or manual replacement
    return template
```

Or use Python's built-in `string.Template` for simple variable substitution.

### Integration with Main Loop

When `status_server_enabled` is true:
- Start the HTTP server in a daemon thread when the daemon starts
- Server runs independently of the main poll loop
- No impact on polling performance
- Server stops when the daemon stops

### Security

- Server binds to `localhost` only (not exposed to network)
- No authentication required (local-only access)
- Consider adding optional basic auth if needed for shared environments

## Success Criteria

1. Status page accessible at http://localhost:8080
2. Page shows current hunter and predd state
3. Page auto-refreshes every 30 seconds (configurable)
4. JSON API endpoint available at /api/status
5. Responsive design works on mobile
6. Dark mode support
7. Failed issues/PRs are highlighted
8. Links to GitHub issues/PRs work correctly
9. Server starts/stops cleanly with daemon
10. No external dependencies (plain HTML/CSS, no framework)
