#!/usr/bin/env python3
"""Mock Jira Server for predd demos.

Mimics the Jira REST API v2 endpoints that hunter uses.
Runs on http://localhost:8081 by default. No auth required.

Usage:
    python demo/mock_jira.py          # default port 8081
    python demo/mock_jira.py --port 9090
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8081
DEFAULT_REPO = "earthmant/predd-demo"  # overridden by --repo CLI arg

# ---------------------------------------------------------------------------
# Fake data
# ---------------------------------------------------------------------------

SPRINT = {
    "id": 1,
    "name": "DEMO Sprint 1",
    "state": "active",
    "startDate": "2026-05-01T00:00:00.000Z",
    "endDate": "2026-05-28T00:00:00.000Z",
}

EPIC = {
    "key": "DEMO-1",
    "fields": {
        "summary": "Core Platform Hardening",
        "issuetype": {"name": "Epic"},
    },
}

ISSUES = [
    {
        "id": "10001",
        "key": "DEMO-10",
        "fields": {
            "summary": "Add /health endpoint to the API",
            "description": (
                "The API has no health-check endpoint. Add `GET /health` that returns "
                "`{\"status\": \"ok\", \"version\": \"<version>\"}` with HTTP 200.\n\n"
                "capability: platform-api\n\n"
                "Files to change: `app.py`. Add a `/health` route. "
                "The version string should be read from `VERSION` file in the project root.\n\n"
                "Acceptance criteria:\n"
                "- `GET /health` returns 200 with JSON body\n"
                "- `version` field matches contents of `VERSION` file\n"
                "- Existing tests still pass\n"
            ),
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
            "labels": ["predd-demo"],
            "customfield_10014": "DEMO-1",   # epic link
            "customfield_10020": [SPRINT],   # sprint
        },
    },
    {
        "id": "10002",
        "key": "DEMO-11",
        "fields": {
            "summary": "Fix off-by-one error in paginate()",
            "description": (
                "The `paginate()` function in `parser.py` returns one fewer item than expected "
                "on the last page when the total is exactly divisible by `page_size`.\n\n"
                "capability: platform-api\n\n"
                "File to change: `parser.py`, function `paginate(items, page, page_size)`.\n\n"
                "The bug: `end = page * page_size` should be `end = (page + 1) * page_size` "
                "when using 0-based page indexing.\n\n"
                "Acceptance criteria:\n"
                "- `paginate([1..10], page=1, page_size=5)` returns `[6,7,8,9,10]`\n"
                "- `paginate([1..10], page=0, page_size=10)` returns all 10 items\n"
                "- Existing tests still pass, new test added for the edge case\n"
            ),
            "issuetype": {"name": "Bug"},
            "status": {"name": "To Do"},
            "labels": ["predd-demo"],
            "customfield_10014": "DEMO-1",
            "customfield_10020": [SPRINT],
        },
    },
    {
        "id": "10003",
        "key": "DEMO-12",
        "fields": {
            "summary": "Add unit tests for the /items endpoint",
            "description": (
                "The `/items` route in `app.py` has no test coverage. "
                "Add tests to `tests/test_app.py`.\n\n"
                "capability: platform-api\n\n"
                "Tests to add:\n"
                "- `test_get_items_empty` — GET /items with empty store returns `[]`\n"
                "- `test_get_items_returns_all` — GET /items returns all stored items\n"
                "- `test_get_item_not_found` — GET /items/999 returns 404\n\n"
                "Use the existing Flask test client pattern in `tests/test_app.py`.\n"
            ),
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
            "labels": ["predd-demo"],
            "customfield_10014": "DEMO-1",
            "customfield_10020": [SPRINT],
        },
    },
]

ISSUES_BY_KEY = {i["key"]: i for i in ISSUES}

# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class MockJiraHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[mock-jira] {self.address_string()} - {fmt % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)

        # Auth validation
        if path == "/rest/api/2/myself":
            self.send_json({"name": "demo-user", "displayName": "Demo User"})

        # Issue search
        elif path == "/rest/api/2/search":
            self.send_json({
                "total": len(ISSUES),
                "maxResults": 1000,
                "startAt": 0,
                "issues": ISSUES,
            })

        # Single issue
        elif path.startswith("/rest/api/2/issue/"):
            key = path.split("/rest/api/2/issue/")[-1]
            issue = ISSUES_BY_KEY.get(key)
            if issue:
                self.send_json(issue)
            else:
                self.send_json({"errorMessages": [f"Issue {key} not found"]}, status=404)

        # Board list (agile)
        elif path in ("/rest/agile/1.0/board", "/rest/agile/1.0/board/"):
            self.send_json({"values": [{"id": 1, "name": "DEMO Board", "type": "scrum"}]})

        # Sprint list
        elif path.startswith("/rest/agile/1.0/board/") and "/sprint" in path:
            self.send_json({"values": [SPRINT]})

        else:
            self.send_json({"errorMessages": [f"Not found: {path}"]}, status=404)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = PORT
    repo = DEFAULT_REPO

    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        port = int(sys.argv[idx + 1])
    if "--repo" in sys.argv:
        idx = sys.argv.index("--repo")
        repo = sys.argv[idx + 1]

    # Patch the label in all issues to match the target repo slug
    for issue in ISSUES:
        issue["fields"]["labels"] = [repo]

    server = HTTPServer(("localhost", port), MockJiraHandler)
    print(f"Mock Jira running at http://localhost:{port}")
    print(f"Routing issues to repo: {repo}")
    print(f"Serving {len(ISSUES)} issues: {', '.join(ISSUES_BY_KEY)}")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
