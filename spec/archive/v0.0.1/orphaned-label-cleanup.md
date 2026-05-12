# Fix: Orphaned Label Cleanup for All Hunter Labels

## Problem

`scan_orphaned_labels` only removes `{github_user}:in-progress` labels. Crashes during later workflow steps leave `{github_user}:proposal-open` and `{github_user}:implementing` labels on issues with no matching hunter state. These issues appear permanently stuck on GitHub.

## Fix

Extend `scan_orphaned_labels` to clean all hunter-owned labels:

- `{github_user}:in-progress`
- `{github_user}:proposal-open`
- `{github_user}:implementing`
- `{github_user}:awaiting-verification`

For each label, scan issues that have it. If the issue has no matching hunter state entry (or state is `submitted`/`failed`), remove the label.

## Also: Run Every N Cycles

Currently runs only at startup. Should also run every 10 poll cycles (configurable) to catch labels orphaned during the current uptime without restarting.

## Config

```toml
orphan_scan_interval = 10  # poll cycles between orphan scans (0 = startup only)
```
