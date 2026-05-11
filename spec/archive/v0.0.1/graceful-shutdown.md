# Graceful Shutdown

## Problem

When predd receives SIGTERM or SIGINT while mid-review, it exits immediately via `sys.exit(0)`. The in-flight PR is left as `status: reviewing` in state, and the child subprocess (`claude`/`devin`) becomes an orphan. On restart, the PR gets re-reviewed from scratch.

## Proposed Behaviour

On SIGTERM/SIGINT:

1. Set a `_stop` flag — do not start any new PR reviews.
2. If a subprocess is currently running, let it finish naturally.
3. Once the subprocess exits (success or failure), update state normally and then exit.
4. If a second signal arrives while waiting, kill the child process, roll the PR state back to unprocessed (delete the state entry), and exit immediately.

## Implementation Notes

- Replace `sys.exit(0)` in `_shutdown` with setting a module-level `threading.Event` or a simple bool flag.
- Track the active `subprocess.Popen` handle so the second-signal handler can terminate it.
- `process_pr` already writes `status: reviewing` at the start — rollback means deleting that key from state and saving before exit.
- The poll loop checks the flag before starting each new PR, so between-PR kills are already instant.

## UX

```
^C
INFO Finishing current review before exiting (^C again to force quit)...
INFO Review posted for fusion-e/ai-bp-toolkit#195
INFO Shutting down cleanly.
```

```
^C^C
WARNING Force quit — rolling back fusion-e/ai-bp-toolkit#195 to unprocessed
INFO Shutting down.
```

## Tradeoffs

- Reviews can take 5–15 minutes. A single Ctrl+C will wait up to that long.
- The "^C again to force quit" pattern is familiar (git, npm) and sets expectations.
- Force quit leaves no orphan process and no stuck state entry.
