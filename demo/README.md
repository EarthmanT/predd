# predd demo

Show the full predd + hunter loop in ~20 minutes using a mock Jira server and a throwaway GitHub repo.

## What the demo shows

1. Hunter ingests issues from mock Jira
2. Hunter creates GitHub issues in the demo repo automatically
3. Hunter claims an issue, writes a proposal PR
4. You merge the proposal
5. Hunter implements it, self-reviews, marks ready
6. You merge the impl PR — hunter closes the issue

## Setup (one-time)

**1. Create the throwaway repo + push starter code**

```bash
bash demo/setup_demo_repo.sh
```

Creates `{you}/predd-demo` on GitHub with a small intentionally-incomplete Python API. Prints the config snippet to add.

**2. Start the mock Jira server**

```bash
python demo/mock_jira.py --repo {you}/predd-demo
```

Runs on `http://localhost:8081`. Serves 3 synthetic issues routed to `predd-demo`.

**3. Add the config block printed by setup_demo_repo.sh to `~/.config/predd/config.toml`**

Also update top-level Jira settings:
```toml
jira_base_url = "http://localhost:8081"
jira_projects = ["DEMO"]
jira_sprint_filter = "active"
```

**4. Restart**

```bash
./start.sh
```

**5. Watch**

```bash
tail -f ~/.config/predd/hunter-log.txt
```

## Demo timeline

| Time | What happens |
|------|-------------|
| 0:00 | Services start |
| ~1:30 | Jira ingest — 3 GitHub issues created in predd-demo |
| ~3:00 | Hunter claims first issue, starts proposal skill |
| ~8:00 | Proposal PR opens as draft |
| You | Review and merge the proposal PR |
| ~2min | Hunter starts implementation |
| ~10min | Impl PR opens, self-reviewed, marked ready |
| You | Merge impl PR |
| ~1:30 | Hunter closes the issue |

## Demo issues

| Key | Summary | What the AI builds |
|-----|---------|-------------------|
| DEMO-10 | Add `/health` endpoint | New Flask route returning `{"status":"ok","version":"..."}` |
| DEMO-11 | Fix off-by-one in `paginate()` | One-line fix + new test |
| DEMO-12 | Add tests for `/items` | 3 new test cases |

## Cleanup

```bash
bash demo/teardown_demo_repo.sh
# Restore real jira_base_url / jira_projects in config.toml
./start.sh
```
