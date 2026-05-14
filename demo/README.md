# predd demo

Show the full predd + hunter loop in ~20 minutes using a mock Jira server against your real repo.

## What the demo shows

1. Hunter ingests issues from Jira (mock server)
2. Hunter creates GitHub issues in your real repo automatically
3. Hunter claims an issue, writes a proposal PR
4. You merge the proposal
5. Hunter implements it, self-reviews, marks ready
6. You merge the impl PR — hunter closes the issue

## Setup (one-time)

**1. Start the mock Jira server**

```bash
python demo/mock_jira.py &
```

Runs on `http://localhost:8081`. Serves 3 synthetic issues against `fusion-e/ai-bp-toolkit`.

**2. Update `~/.config/predd/config.toml`**

Change the Jira settings temporarily:

```toml
jira_base_url = "http://localhost:8081"
jira_api_enabled = true
jira_projects = ["DEMO"]
jira_sprint_filter = "active"
```

**3. Restart**

```bash
./start.sh
```

**4. Watch**

```bash
tail -f ~/.config/predd/hunter-log.txt
```

## Demo timeline

| Time | What happens |
|------|-------------|
| 0:00 | Services start, hunter polls |
| ~1:30 | Jira ingest runs — 3 GitHub issues created |
| ~3:00 | Hunter claims first issue, starts proposal skill |
| ~8:00 | Proposal PR opens as draft |
| You | Review and merge the proposal PR |
| ~2min | Hunter detects merge, starts implementation |
| ~10min | Impl PR opens, hunter self-reviews, marks ready |
| You | Merge the impl PR |
| ~1:30 | Hunter closes the GitHub issue |

## Demo issues

| Key | Summary |
|-----|---------|
| DEMO-10 | Add `/health` endpoint to the API |
| DEMO-11 | Fix off-by-one error in `paginate()` |
| DEMO-12 | Add unit tests for the `/items` endpoint |

Edit `demo/mock_jira.py` to change the issues to something more relevant to what you want to show.

## Cleanup

```bash
# Stop mock Jira
pkill -f mock_jira.py

# Restore real Jira config
# Edit ~/.config/predd/config.toml — restore jira_base_url and jira_projects

./start.sh
```
