# Demo Mode

## Status: pending

## Goal

Allow a first-time observer (e.g. a manager) to see the full predd + hunter loop working end-to-end in under 30 minutes, without needing a real Jira instance or a real work repo.

---

## Components

### 1. Mock Jira Server (`demo/mock_jira.py`)

A lightweight HTTP server that mimics the Jira REST API endpoints hunter uses:

| Endpoint | Behaviour |
|----------|-----------|
| `GET /rest/api/2/search` | Returns a fixed list of fake issues (JQL ignored) |
| `GET /rest/api/2/issue/{key}` | Returns a single fake issue by key |
| `GET /rest/agile/1.0/board` | Returns a fake board |
| `GET /rest/agile/1.0/board/{id}/sprint` | Returns one active sprint |

Issues returned are realistic but synthetic — e.g. "Add a health-check endpoint", "Write unit tests for the parser", "Fix off-by-one error in pagination". Each has an epic, sprint, and capability field populated so conformance checks pass.

Runs on `localhost:8081`. No auth required (hunter should accept any token against it).

Config to point hunter at the mock:

```toml
jira_base_url = "http://localhost:8081"
jira_api_enabled = true
jira_projects = ["DEMO"]
jira_sprint_filter = "active"
```

### 2. Demo Repo Setup (`demo/setup_demo_repo.sh`)

A script the user runs once to prepare a throwaway GitHub repo in their personal account:

1. Creates a new public repo `{github_user}/predd-demo` via `gh repo create`
2. Pushes a small but realistic starter codebase — a minimal Python web service with:
   - `app.py` — a Flask app with a few routes
   - `parser.py` — a simple data parser
   - `tests/` — a couple of existing unit tests
   - `README.md`
3. Creates the Jira issue labels hunter expects (`DAP09A` → `DEMO`)
4. Prints the config snippet to add to `~/.config/predd/config.toml`

The dummy codebase is intentionally incomplete so the demo issues have something real to implement (missing health-check route, missing test coverage, an obvious off-by-one bug).

### 3. Demo Config Snippet

After running setup, the user adds one `[[repo]]` block:

```toml
[[repo]]
name = "{github_user}/predd-demo"
predd = true
hunter = true
obsidian = false
```

And updates top-level Jira settings to point at the mock server.

### 4. Demo Teardown (`demo/teardown_demo_repo.sh`)

Deletes the GitHub repo and removes the `[[repo]]` block reminder.

---

## Demo flow (what the boss sees)

1. `python demo/mock_jira.py &` — start mock Jira
2. `bash demo/setup_demo_repo.sh` — create repo + push dummy code
3. Add config snippet, `./start.sh`
4. Watch `tail -f ~/.config/predd/hunter-log.txt`
5. Within one poll cycle: hunter ingests Jira issues → creates GitHub issues
6. Within the next cycle: hunter claims an issue, creates a proposal PR
7. User merges proposal PR
8. Hunter creates implementation PR, self-reviews, marks ready
9. User merges impl PR → hunter closes the issue

Total elapsed: ~20–30 minutes depending on model speed.

---

## Files

```
demo/
  mock_jira.py           # mock Jira HTTP server
  setup_demo_repo.sh     # create throwaway GH repo + push dummy code
  teardown_demo_repo.sh  # cleanup
  dummy_codebase/
    app.py
    parser.py
    tests/
      test_parser.py
    README.md
```

---

## Out of scope

- Mock GitHub API (use the real one with a throwaway repo)
- Automated demo runner / CI
- Persistent mock Jira state (issues are always the same fixed set)
