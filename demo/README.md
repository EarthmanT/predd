# predd demo

Run the full predd + hunter loop in ~20 minutes using a mock Jira server and a throwaway GitHub repo.

## What the demo shows

1. Hunter ingests issues from Jira (mock server)
2. Hunter creates GitHub issues automatically
3. Hunter claims an issue, writes a proposal PR
4. You merge the proposal
5. Hunter implements it, self-reviews, marks ready
6. You merge the impl PR — hunter closes the issue

## Setup (one-time)

**1. Start the mock Jira server**

```bash
python demo/mock_jira.py &
```

Runs on `http://localhost:8081`. Serves 3 synthetic issues (health endpoint, pagination bug fix, test coverage).

**2. Create the demo GitHub repo**

```bash
bash demo/setup_demo_repo.sh
```

This creates `{you}/predd-demo` with a small intentionally-incomplete Python API and prints the config snippet to add.

**3. Add to `~/.config/predd/config.toml`**

The setup script prints the exact block. It adds `predd-demo` as a watched repo and points Jira at `localhost:8081`.

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
| 0:00 | Services start, hunter polls |
| ~1:30 | Jira ingest runs — 3 GitHub issues created in predd-demo |
| ~3:00 | Hunter claims first issue, starts proposal skill |
| ~8:00 | Proposal PR opens as draft |
| You | Review and merge the proposal PR |
| ~2min | Hunter detects merge, starts implementation |
| ~10min | Impl PR opens, hunter self-reviews, marks ready |
| You | Merge the impl PR |
| ~1:30 | Hunter closes the GitHub issue |

## Teardown

```bash
bash demo/teardown_demo_repo.sh
```

Deletes the GitHub repo and reminds you to remove the config block.

## Demo issues

| Key | Summary | What gets built |
|-----|---------|-----------------|
| DEMO-10 | Add `/health` endpoint | New Flask route in `app.py` |
| DEMO-11 | Fix off-by-one in `paginate()` | One-line fix in `parser.py` + test |
| DEMO-12 | Add tests for `/items` | 3 new test cases in `tests/test_app.py` |
